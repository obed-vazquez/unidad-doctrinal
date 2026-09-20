#!/usr/bin/env python3
"""Post-procesa los HTML exportados de Google Docs para que sean indexables.

El repositorio publica documentación *generada*: el HTML se regenera desde Drive
y se copia tal cual, así que no se edita en sitio. Este script lee de --src y
escribe una copia tratada en --out; el repo nunca guarda HTML modificado.

Uso:
    python scripts/seo_postprocess.py --src . --out _site
    python scripts/seo_postprocess.py --src . --out _site --dry-run
    python scripts/seo_postprocess.py --src . --out _site --check

Sin dependencias externas: corre igual en local y en CI.
"""

from __future__ import annotations

import argparse
import fnmatch
import html
import re
import shutil
import sys
from pathlib import Path
from urllib.parse import quote

# Lista de documentos que no se publican. Vive en un archivo aparte para poder
# editarla sin tocar el script.
EXCLUDE_FILE = Path(__file__).resolve().parent / "excluidos.txt"
# Títulos que legítimamente salen del nombre de archivo, ya revisados a mano.
FALLBACK_OK_FILE = Path(__file__).resolve().parent / "titulos-por-nombre.txt"

BASE_URL = "https://unidad.whiteweb.mx"

# Carpetas que nunca se publican.
SKIP_DIRS = {
    ".git",
    ".github",
    ".idea",
    "scripts",
    "__pycache__",
    ".obsidian",
    # Salidas del propio script: van dentro del repo pero ignoradas por git, así
    # que hay que saltarlas para no re-procesar lo ya generado.
    "_site",
    "_preview",
}

# Documentos internos (plantillas, borradores) que no deben indexarse: no aportan
# contenido y diluyen el sitio ante el buscador.
NOINDEX_PATTERNS = (
    re.compile(r"_template_|_plantilla_", re.I),
    re.compile(r"\btemplate\b", re.I),
    re.compile(r"^untitled document$", re.I),
)

DESC_MAX = 155
DESC_MIN = 80  # Por debajo de esto un bloque es encabezado, no prosa.
TITLE_MAX = 120
SITE_NAME = "Unidad Doctrinal"

# El bloque de metadatos que Docs deja tras el título: "[Folder, <enlaces>]".
META_BRACKET = re.compile(r"\s*\[.*$", re.S)

RE_SUBTITLE = re.compile(r'<p class="subtitle"[^>]*>(.*?)</p>', re.S | re.I)
# El cuerpo de estos documentos vive en listas anidadas, no en párrafos.
RE_BLOCK = re.compile(r"<(p|li|h[1-6])\b[^>]*>(.*?)</\1>", re.S | re.I)
# Enlaces sueltos y notas de trabajo del autor ("P:" pregunta, "R:" respuesta).
RE_NOISE = re.compile(r"^(https?://|P:|R:|Nota:|#)", re.I)
RE_DEFINICION = re.compile(r"^definici[oó]n\b", re.I)
RE_TAG = re.compile(r"<[^>]+>")
RE_HEAD_OPEN = re.compile(r"<head[^>]*>", re.I)
RE_HTML_OPEN = re.compile(r"<html(?![^>]*\blang=)([^>]*)>", re.I)
RE_HAS_TITLE = re.compile(r"<title\s*>", re.I)
RE_HAS_VIEWPORT = re.compile(r'<meta[^>]+name=["\']viewport', re.I)
RE_HAS_CANONICAL = re.compile(r'<link[^>]+rel=["\']canonical', re.I)

# Numeración de los índices: "0) ", "1) ", "2_) ". Exige el paréntesis: sin él
# se comería números que son parte del título ("70 PROFECÍAS CUMPLIDAS…").
RE_FILE_PREFIX = re.compile(r"^\s*\d+\s*_?\)\s*")
RE_FILE_SUFFIX = re.compile(r"\.(pdf|docx?|xlsx?)$", re.I)


def text_of(fragment: str) -> str:
    """Quita etiquetas, resuelve entidades y colapsa espacios."""
    plain = RE_TAG.sub("", fragment)
    plain = html.unescape(plain)
    return re.sub(r"[\s ]+", " ", plain).strip()


def title_from_filename(path: Path) -> str:
    stem = RE_FILE_SUFFIX.sub("", RE_FILE_PREFIX.sub("", path.stem))
    return stem.replace("_", " ").strip(" -–—") or path.stem


def extract_title(source: str, path: Path) -> tuple[str, str]:
    """Devuelve (título, origen). El origen alimenta el reporte de --dry-run.

    El estilo "Subtítulo" de Docs es el que carga el nombre real del documento;
    el estilo "Título" está mal aplicado sobre párrafos de cuerpo en varios
    archivos, así que no se usa como fuente.
    """
    match = RE_SUBTITLE.search(source)
    if match:
        candidate = META_BRACKET.sub("", text_of(match.group(1))).strip(" -–—·")
        if 3 <= len(candidate) <= TITLE_MAX:
            return candidate, "subtitle"
    return title_from_filename(path), "filename"


def extract_description(source: str, title: str) -> tuple[str, str]:
    """Devuelve (descripción, origen).

    Varios documentos abren con un bullet "Definición" seguido de la definición
    canónica: ese es el mejor resumen disponible, así que tiene prioridad sobre
    el primer bloque largo.
    """
    title_head = title.lower()[:20]
    after_definicion = False
    fallback = ""

    for match in RE_BLOCK.finditer(source):
        body = text_of(match.group(2))
        if not body or "[Folder" in body or body.lower().startswith(title_head):
            continue
        if RE_NOISE.match(body):
            continue
        if RE_DEFINICION.match(body):
            after_definicion = True
            continue
        if len(body) < DESC_MIN:
            continue
        if after_definicion:
            return truncate(body, DESC_MAX), "definición"
        if not fallback:
            fallback = body

    if fallback:
        return truncate(fallback, DESC_MAX), "primer bloque"
    return (
        truncate(f"{title} — documento de estudio doctrinal de {SITE_NAME}.", DESC_MAX),
        "genérica",
    )


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(" ,;:.") + "…"


def is_noindex(path: Path) -> bool:
    return any(pattern.search(path.stem) for pattern in NOINDEX_PATTERNS)


def canonical_for(rel: Path, base_url: str) -> str:
    parts = [quote(p) for p in rel.parts]
    if parts[-1].lower() == "index.html":
        parts = parts[:-1]
    return base_url.rstrip("/") + "/" + "/".join(parts)


def build_head(title: str, description: str, canonical: str, noindex: bool) -> str:
    esc_title = html.escape(title, quote=True)
    esc_desc = html.escape(description, quote=True)
    tags = [
        f"<title>{html.escape(title)}</title>",
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f'<meta name="description" content="{esc_desc}">',
        f'<link rel="canonical" href="{canonical}">',
        f'<meta property="og:title" content="{esc_title}">',
        f'<meta property="og:description" content="{esc_desc}">',
        f'<meta property="og:url" content="{canonical}">',
        '<meta property="og:type" content="article">',
        f'<meta property="og:site_name" content="{SITE_NAME}">',
    ]
    if noindex:
        tags.insert(0, '<meta name="robots" content="noindex,follow">')
    return "\n" + "\n".join(tags) + "\n"


def process_html(source: str, rel: Path, base_url: str) -> tuple[str, dict]:
    title, origin = extract_title(source, rel)
    description, desc_origin = extract_description(source, title)
    canonical = canonical_for(rel, base_url)
    noindex = is_noindex(rel)

    result = source
    # Idempotente: si ya trae <title>, este archivo ya pasó por aquí.
    if not RE_HAS_TITLE.search(result):
        head = build_head(title, description, canonical, noindex)
        match = RE_HEAD_OPEN.search(result)
        if match:
            result = result[: match.end()] + head + result[match.end() :]
        else:
            result = f"<head>{head}</head>" + result
    if not re.search(r"<html[^>]*\blang=", result, re.I):
        result = RE_HTML_OPEN.sub(r'<html lang="es"\1>', result, count=1)

    return result, {
        "title": title,
        "origin": origin,
        "description": description,
        "desc_origin": desc_origin,
        "canonical": canonical,
        "noindex": noindex,
    }


def build_sitemap(entries: list[str]) -> str:
    urls = "\n".join(f"  <url><loc>{html.escape(u)}</loc></url>" for u in sorted(entries))
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{urls}\n"
        "</urlset>\n"
    )


def build_robots(base_url: str) -> str:
    return f"User-agent: *\nAllow: /\n\nSitemap: {base_url.rstrip('/')}/sitemap.xml\n"


def load_patterns(path: Path) -> list[str]:
    """Lee una lista de rutas/comodines, ignorando comentarios y líneas vacías."""
    if not path.exists():
        return []
    patterns = []
    # utf-8-sig: un editor de Windows puede guardar estos archivos con BOM, y el
    # BOM quedaría pegado al primer patrón, que dejaría de coincidir en silencio.
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            patterns.append(line)
    return patterns


def matching_pattern(rel: Path, patterns: list[str]) -> str | None:
    target = rel.as_posix()
    for pattern in patterns:
        if fnmatch.fnmatch(target, pattern):
            return pattern
    return None


def iter_html(src: Path):
    for path in sorted(src.rglob("*.html")):
        if any(part in SKIP_DIRS for part in path.relative_to(src).parts):
            continue
        yield path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--src", type=Path, default=Path("."))
    parser.add_argument("--out", type=Path, default=Path("_site"))
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument(
        "--exclude-file",
        type=Path,
        default=EXCLUDE_FILE,
        help="Lista de documentos que no se publican (default: scripts/excluidos.txt).",
    )
    parser.add_argument(
        "--fallback-ok-file",
        type=Path,
        default=FALLBACK_OK_FILE,
        help=(
            "Documentos cuyo título sale del nombre de archivo y ya fueron revisados. "
            "No se reportan ni cuentan para --max-fallback."
        ),
    )
    parser.add_argument("--dry-run", action="store_true", help="No escribe; solo reporta.")
    parser.add_argument("--check", action="store_true", help="Falla si falta algún meta.")
    parser.add_argument(
        "--max-fallback",
        type=int,
        default=None,
        help=(
            "Falla si más de N títulos salen del nombre de archivo. Detecta que el "
            "Apps Script dejó de emitir el estilo 'Subtítulo': sin esto la degradación "
            "es silenciosa, porque el fallback siempre produce algún título."
        ),
    )
    args = parser.parse_args()

    src = args.src.resolve()
    out = args.out.resolve()
    if not src.is_dir():
        print(f"error: --src no existe: {src}", file=sys.stderr)
        return 2
    if out == src:
        print("error: --out no puede ser igual a --src", file=sys.stderr)
        return 2

    if not args.dry_run:
        if out.exists():
            shutil.rmtree(out)
        out.mkdir(parents=True)

    exclusions = load_patterns(args.exclude_file)
    fallback_ok = load_patterns(args.fallback_ok_file)
    excluded: list[str] = []
    used_patterns: set[str] = set()
    used_fallback_ok: set[str] = set()
    fallback_new: list[tuple[str, str]] = []
    fallback_known = 0
    from_subtitle = from_filename = noindexed = 0
    desc_origins: dict[str, int] = {}
    sitemap_entries: list[str] = []
    failures: list[str] = []
    rows: list[tuple[str, str, str]] = []

    for path in iter_html(src):
        if out in path.parents:
            continue
        rel = path.relative_to(src)
        pattern = matching_pattern(rel, exclusions)
        if pattern:
            excluded.append(str(rel))
            used_patterns.add(pattern)
            continue
        source = path.read_text(encoding="utf-8-sig", errors="replace")
        result, info = process_html(source, rel, args.base_url)

        if info["origin"] == "subtitle":
            from_subtitle += 1
        else:
            from_filename += 1
            approved = matching_pattern(rel, fallback_ok)
            if approved:
                fallback_known += 1
                used_fallback_ok.add(approved)
            else:
                fallback_new.append((str(rel), info["title"]))
        desc_origins[info["desc_origin"]] = desc_origins.get(info["desc_origin"], 0) + 1
        if info["noindex"]:
            noindexed += 1
        else:
            sitemap_entries.append(info["canonical"])

        if args.check:
            for label, pattern in (
                ("title", RE_HAS_TITLE),
                ("viewport", RE_HAS_VIEWPORT),
                ("canonical", RE_HAS_CANONICAL),
            ):
                if not pattern.search(result):
                    failures.append(f"{rel}: falta {label}")

        rows.append((str(rel), info["origin"], info["title"]))

        if not args.dry_run:
            target = out / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(result, encoding="utf-8")

    if not args.dry_run:
        (out / "sitemap.xml").write_text(build_sitemap(sitemap_entries), encoding="utf-8")
        (out / "robots.txt").write_text(build_robots(args.base_url), encoding="utf-8")
        (out / ".nojekyll").write_text("", encoding="utf-8")
        cname = src / "CNAME"
        if cname.exists():
            shutil.copy2(cname, out / "CNAME")

    total = from_subtitle + from_filename
    print(f"procesados        : {total}")
    print(f"  título del doc  : {from_subtitle}")
    print(f"  título del name : {from_filename} ({fallback_known} revisados, {len(fallback_new)} nuevos)")
    print(f"excluidos         : {len(excluded)}")
    print(f"noindex (internos): {noindexed}")
    print(f"sitemap           : {len(sitemap_entries)} URLs")
    print("descripciones     : " + ", ".join(f"{k}={v}" for k, v in sorted(desc_origins.items())))

    if excluded:
        print("\nExcluidos del sitio:")
        for rel in excluded:
            print(f"  {rel}")

    if fallback_new:
        print("\nTítulos derivados del nombre de archivo (revisar):")
        for rel, title in fallback_new:
            print(f"  {title}   <- {rel}")
        print(f"\nSi se ven bien, agrégalos a {args.fallback_ok_file.name} para no volver a avisarte.")

    # Un patrón que no coincide con nada suele ser un documento renombrado en la
    # regeneración: la exclusión dejó de aplicar y el documento se publicaría.
    stale = [p for p in exclusions if p not in used_patterns]
    if stale and args.check:
        for pattern in stale:
            failures.append(f"exclusión sin coincidencias: {pattern}")

    # Una entrada de la whitelist sin uso es inofensiva: el documento recuperó su
    # título propio. Se avisa para poder limpiarla, pero no rompe la build.
    stale_ok = [p for p in fallback_ok if p not in used_fallback_ok]
    if stale_ok:
        print(f"\nEntradas de {args.fallback_ok_file.name} que ya no aplican (puedes borrarlas):")
        for pattern in stale_ok:
            print(f"  {pattern}")

    if args.max_fallback is not None and len(fallback_new) > args.max_fallback:
        failures.append(
            f"{len(fallback_new)} títulos nuevos vienen del nombre de archivo "
            f"(máximo tolerado: {args.max_fallback}); revisa si el export de Drive cambió"
        )

    if failures:
        print(f"\nFALLAS ({len(failures)}):", file=sys.stderr)
        for failure in failures[:40]:
            print(f"  {failure}", file=sys.stderr)
        return 1

    if args.check:
        print("\ncheck: OK — todos los documentos traen title, viewport y canonical.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
