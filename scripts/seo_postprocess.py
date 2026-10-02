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
import json
import os
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
# Fragmento de analítica; solo se inyecta con --analytics (lo pasa el workflow).
ANALYTICS_FILE = Path(__file__).resolve().parent / "analytics.html"
# Preguntas por documento, generadas según scripts/REGENERAR-PREGUNTAS.md.
PREGUNTAS_FILE = Path(__file__).resolve().parent / "preguntas.json"
# Documentos que se publican sin recuadro de preguntas, a propósito.
SIN_PREGUNTAS_FILE = Path(__file__).resolve().parent / "sin-preguntas.txt"

PREGUNTAS_TITULO = "Preguntas que responde este documento"

# Se imprime cuando el check falla por preguntas faltantes. Va dirigido a quien
# integra el PR, que puede no ser quien montó esto.
COMO_REGENERAR = """
────────────────────────────────────────────────────────────────────────
  QUÉ HACER CON ESTO
────────────────────────────────────────────────────────────────────────
  Llegaron documentos que todavía no tienen preguntas en el caché. Las
  preguntas son el bloque "Preguntas que responde este documento" que el
  sitio muestra arriba de cada página, y son lo que hace que un documento
  aparezca cuando alguien busca esa pregunta exacta.

  El caché NO se escribe a mano: lo genera una LLM leyendo los documentos.

  1. Abre este repositorio con una LLM (Claude Code, por ejemplo) y pídele:

       "Regenera el caché de preguntas siguiendo
        scripts/REGENERAR-PREGUNTAS.md"

     Ese archivo tiene las instrucciones completas: cómo leer los
     documentos, cómo redactar las preguntas y cómo validarlas.

  2. Revisa el diff de scripts/preguntas.json antes de aprobar el PR.
     Una pregunta que el documento no responde es peor que ninguna.

  3. Si algún documento no debe llevar preguntas — un índice, una
     plantilla sin llenar, un proceso interno — agrégalo a
     scripts/sin-preguntas.txt en vez de inventarle preguntas.

  Para ver el detalle de qué falta y de qué tamaño es cada documento:

     python scripts/seo_postprocess.py --src . --out _preview \\
       --dry-run --reporte-preguntas
────────────────────────────────────────────────────────────────────────
"""

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
# Igual que el anterior, pero conservando los atributos para reetiquetar a <h1>.
RE_SUBTITLE_TAG = re.compile(r'<p class="subtitle"([^>]*)>(.*?)</p>', re.S | re.I)
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


def process_html(
    source: str,
    rel: Path,
    base_url: str,
    analytics: str = "",
    preguntas: list[str] | None = None,
) -> tuple[str, dict]:
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

    # El título del documento viaja como <p class="subtitle">, y los <h1> que
    # trae el export son etiquetas de sección, no encabezados de página. Se
    # promueve el título a <h1> y se baja TODO lo demás un nivel para hacerle
    # sitio, en una sola pasada para que nada se mueva dos veces.
    #
    # Los <h6> se quedan donde están: HTML no tiene h7. Eso deja un choque entre
    # h5 y h6, pero solo en 7 documentos y en el nivel más profundo — mucho menos
    # dañino que el anterior, que aplastaba secciones principales con sus propias
    # subsecciones en 69 documentos.
    falta_h1 = False
    if "ud-titulo" not in result:
        cuerpo_ini = result.find("<body")
        if cuerpo_ini != -1:
            cabeza, cuerpo = result[:cuerpo_ini], result[cuerpo_ini:]
            cuerpo = re.sub(
                r"<(/?)h([1-6])\b",
                lambda m: f"<{m.group(1)}h{min(int(m.group(2)) + 1, 6)}",
                cuerpo,
                flags=re.I,
            )
            cuerpo, n = RE_SUBTITLE_TAG.subn(
                r'<h1 class="ud-titulo"\1>\2</h1>', cuerpo, count=1
            )
            result = cabeza + cuerpo
            # Sin subtítulo que promover no hay nada que reetiquetar, así que el
            # encabezado se inyecta más abajo, ya con el título calculado.
            falta_h1 = not n

    if preguntas and "ud-preguntas" not in result:
        visible, ld = build_preguntas(preguntas, canonical)
        match = re.search(r"<body[^>]*>", result, re.I)
        if match:
            result = result[: match.end()] + visible + result[match.end() :]
        match = RE_HEAD_OPEN.search(result)
        if match:
            result = result[: match.end()] + "\n" + ld + "\n" + result[match.end() :]

    # Va después del bloque de preguntas para quedar por encima de él: ambos se
    # insertan justo tras <body>, así que el último en insertarse queda primero.
    if falta_h1:
        match = re.search(r"<body[^>]*>", result, re.I)
        if match:
            encabezado = (
                '<h1 class="ud-titulo" style="margin:0 0 .6em;color:#2e5b65;'
                'font-weight:300;font-size:15pt;font-family:Ubuntu,Arial,sans-serif">'
                f"{html.escape(title)}</h1>"
            )
            result = result[: match.end()] + encabezado + result[match.end() :]

    # Idempotente igual que el resto: el marcador del fragmento evita duplicarlo
    # aunque cambien las herramientas de analítica que contiene.
    if analytics and "ud-analytics" not in result:
        match = RE_HEAD_OPEN.search(result)
        if match:
            result = result[: match.end()] + "\n" + analytics.strip() + "\n" + result[match.end() :]

    return result, {
        "title": title,
        "origin": origin,
        "description": description,
        "desc_origin": desc_origin,
        "canonical": canonical,
        "noindex": noindex,
    }


def texto_del_cuerpo(source: str) -> int:
    """Longitud del texto real del documento, sin el CSS embebido.

    Medir sobre el HTML crudo engaña: los exports de Docs llevan decenas de KB
    de CSS dentro de <style>, y eso no es contenido.
    """
    cuerpo = re.sub(r"<(style|script)\b.*?</\1>", "", source, flags=re.S | re.I)
    return sum(len(text_of(m.group(2))) for m in RE_BLOCK.finditer(cuerpo))


def build_preguntas(preguntas: list[str], canonical: str) -> str:
    """Bloque visible + JSON-LD FAQPage.

    El bloque va visible a propósito: los buscadores puntúan sobre el texto que
    el lector ve, y las etiquetas invisibles llevan dos décadas ignoradas. El
    JSON-LD acompaña porque es lo que leen los extractores de los LLM.
    """
    items = "".join(f"<li>{html.escape(q)}</li>" for q in preguntas)
    visible = (
        '<section class="ud-preguntas" style="margin:0 0 1.5em;padding:.75em 1em;'
        'border-left:3px solid #45818e;background:#f6f8f9;font-size:10pt;'
        'font-family:Ubuntu,Arial,sans-serif;color:#444">'
        f'<strong style="display:block;margin-bottom:.35em;color:#2e5b65">{PREGUNTAS_TITULO}</strong>'
        f'<ul style="margin:0;padding-left:1.2em">{items}</ul>'
        "</section>"
    )
    faq = {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "url": canonical,
        "mainEntity": [
            {"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "url": canonical}}
            for q in preguntas
        ],
    }
    ld = json.dumps(faq, ensure_ascii=False, indent=None)
    return visible, f'<script type="application/ld+json">{ld}</script>'


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
    parser.add_argument(
        "--analytics",
        action="store_true",
        help=(
            "Inyecta scripts/analytics.html en cada documento. Apagado por defecto "
            "para que la vista previa local no mande visitas falsas; lo activa el workflow."
        ),
    )
    parser.add_argument(
        "--reporte-preguntas",
        action="store_true",
        help="Lista cada documento con su tamaño, preguntas en caché y cuántas le tocan.",
    )
    parser.add_argument(
        "--sin-preguntas-file",
        type=Path,
        default=SIN_PREGUNTAS_FILE,
        help="Documentos que se publican sin recuadro de preguntas a propósito.",
    )
    parser.add_argument(
        "--estricto",
        action="store_true",
        help=(
            "Trata los avisos como errores. Los avisos señalan cosas que alguien "
            "debería mirar (una entrada obsoleta, un conteo fuera de referencia), "
            "no roturas; con esto el PR no pasa hasta resolverlos."
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
    preguntas_cache: dict[str, list[str]] = {}
    if PREGUNTAS_FILE.exists():
        datos = json.loads(PREGUNTAS_FILE.read_text(encoding="utf-8-sig"))
        preguntas_cache = datos.get("documentos", {})
    sin_preguntas_ok = load_patterns(args.sin_preguntas_file)
    filas_preguntas: list[tuple[str, int, int, int, int]] = []
    con_preguntas = 0
    exentos_preguntas = 0
    faltan_preguntas: list[str] = []
    avisos: list[str] = []
    instrucciones = False

    analytics_snippet = ""
    if args.analytics:
        if not ANALYTICS_FILE.exists():
            print(f"error: falta {ANALYTICS_FILE}", file=sys.stderr)
            return 2
        analytics_snippet = ANALYTICS_FILE.read_text(encoding="utf-8-sig")
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
        rel_key = rel.as_posix()
        preguntas = preguntas_cache.get(rel_key, [])
        result, info = process_html(source, rel, args.base_url, analytics_snippet, preguntas)

        filas_preguntas.append((rel_key, texto_del_cuerpo(source), len(preguntas)))
        if preguntas:
            con_preguntas += 1
        elif info["noindex"] or matching_pattern(rel, sin_preguntas_ok):
            # Exento: o no se indexa, o está listado a propósito.
            exentos_preguntas += 1
        else:
            faltan_preguntas.append(rel_key)

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
    print(f"preguntas         : {con_preguntas} con, {exentos_preguntas} exentos, {len(faltan_preguntas)} faltan")

    if args.reporte_preguntas:
        print("\nReporte de preguntas (para regenerar el caché):")
        print("  El tamaño es informativo — cuántas preguntas merece cada documento")
        print("  lo decide quien lo lee, no su longitud.")
        print(f"  {'texto':>8}  {'preg.':>6}  documento")
        for rel_key, tam, tiene in filas_preguntas:
            print(f"  {tam:>8}  {tiene:>6}  {rel_key}")

    # Avisos blandos: cosas que alguien debería mirar, pero que no son errores.
    # En GitHub Actions se emiten como ::warning:: para que salgan anotadas en el
    # PR en vez de quedar enterradas en el log.
    def avisar(mensaje: str) -> None:
        avisos.append(mensaje)
        if os.environ.get("GITHUB_ACTIONS"):
            print(f"::warning::{mensaje}")

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
    for pattern in stale_ok:
        avisar(f"{args.fallback_ok_file.name}: la entrada '{pattern}' ya no aplica; puedes borrarla")

    # Un documento publicado sin preguntas y sin estar exento es, casi siempre,
    # material nuevo que llegó de Drive y que nadie ha mirado todavía.
    if faltan_preguntas and args.check:
        failures.append(
            f"{len(faltan_preguntas)} documentos sin preguntas y sin estar en "
            f"{args.sin_preguntas_file.name}"
        )
        for rel_key in faltan_preguntas[:15]:
            failures.append(f"    sin preguntas: {rel_key}")
        instrucciones = True

    if args.max_fallback is not None and len(fallback_new) > args.max_fallback:
        failures.append(
            f"{len(fallback_new)} títulos nuevos vienen del nombre de archivo "
            f"(máximo tolerado: {args.max_fallback}); revisa si el export de Drive cambió"
        )

    if avisos:
        print(f"\nAVISOS ({len(avisos)}):")
        for aviso in avisos:
            print(f"  {aviso}")
        if args.estricto:
            failures.append(f"{len(avisos)} avisos, y --estricto los trata como error")

    if failures:
        print(f"\nFALLAS ({len(failures)}):", file=sys.stderr)
        for failure in failures[:40]:
            print(f"  {failure}", file=sys.stderr)
        if instrucciones:
            print(COMO_REGENERAR, file=sys.stderr)
        return 1

    if args.check:
        print("\ncheck: OK — todos los documentos traen title, viewport y canonical.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
