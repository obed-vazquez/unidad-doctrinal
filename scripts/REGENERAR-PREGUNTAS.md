# Regenerar el caché de preguntas

Instrucciones para una LLM (Claude u otra) que deba crear o actualizar
`scripts/preguntas.json`. Este archivo es un **caché**: se genera de forma
deliberada y se revisa en un PR. El post-procesado **no** llama a ningún modelo
— si lo hiciera, el build dejaría de ser reproducible y se publicarían preguntas
que nadie leyó.

## Por qué existe

Los documentos se titulan con etiquetas doctrinales ("La Salvación Bautismal")
pero la gente busca preguntas ("¿el bautismo salva?"). Estas preguntas cierran
esa distancia, para buscadores y para la recuperación de los LLM.

## Pasos

### 1. Ver qué falta

```bash
python scripts/seo_postprocess.py --src . --out _preview --dry-run --reporte-preguntas
```

Columnas: tamaño del texto del cuerpo, preguntas en caché, cuántas le tocan, ruta.
Las filas marcadas con `!` están fuera de rango — son las que hay que atender.

### 2. Leer el documento

No inventes preguntas desde el título. Extrae el texto del documento y léelo:
los documentos son exports de Google Docs, con el cuerpo en listas anidadas
(`<li>`), no en párrafos.

### 3. Escribir las preguntas

**Cuántas: lo decides tú, documento por documento.** No hay tabla ni cuota, y es
deliberado.

El criterio es **cuántas preguntas distintas responde el documento de verdad**.
El tamaño no lo predice: un documento largo puede estar lleno de material
tangencial, referencias y notas de trabajo, y rendir tres preguntas; uno corto y
denso puede cubrir cinco frentes bien diferenciados. Solo quien lee el texto
puede saberlo, y por eso la decisión es tuya.

Dos errores a evitar, en este orden de gravedad:

1. **Inflar la lista para llegar a un número.** Una pregunta que el documento no
   responde es una promesa incumplida: la persona llega, no encuentra lo que
   buscaba, y se va. Eso daña más que no aparecer.
2. **Quedarte corto en un documento desarrollado.** Cada pregunta legítima es una
   búsqueda por la que puedes aparecer; dejarlas fuera desperdicia alcance.

Si dudas, léelo completo antes de decidir. El error más fácil de cometer es
juzgar por un extracto: un documento parece breve, le pones tres preguntas, y
resulta que tenía cuarenta mil caracteres de desarrollo que no viste.

El reporte (`--reporte-preguntas`) muestra el tamaño de cada documento, pero solo
como dato para orientarte sobre cuánto te falta leer — no como una vara contra la
cual medir el resultado.

Cómo redactarlas:

- **Como las teclea una persona**, no como las titula un teólogo.
  `¿el bautismo salva?`, no `Análisis del bautismo constitutivo`
- **Empieza por la pregunta más buscada.** La primera es la que más pesa
- **Que el documento las responda de verdad.** Una pregunta que el texto no
  aborda es una promesa incumplida, y a la larga perjudica
- **Incluye las de versículo concreto** cuando el documento los analiza:
  `¿Qué significa Gálatas 5:4, de la gracia habéis caído?`
- **No prejuzgues la conclusión.** El proyecto trata los temas de forma
  objetiva; la pregunta se formula neutra aunque el documento concluya
- **Sin comillas dobles** en el texto (complican el JSON y el HTML)
- Una sola línea, con sus signos de apertura y cierre

### 4. Escribir el caché

`scripts/preguntas.json`, bajo `documentos`. La clave es la ruta relativa al
repositorio con `/` como separador, tal como la imprime el reporte.

### 5. Validar

```bash
python scripts/seo_postprocess.py --src . --out _preview --check --reporte-preguntas
```

No debe quedar `!` en los documentos que acabas de tratar. Revisa además el
bloque visible en `_preview/` abriendo un documento en el navegador.

## Qué NO hacer

- No pongas preguntas en documentos que son plantillas sin llenar, índices o
  procesos internos. Si te topas con uno, repórtalo: probablemente vaya a
  `excluidos.txt` en vez de recibir preguntas
- No repitas la misma pregunta en dos documentos: competirían entre sí. Si dos
  documentos responden lo mismo, repórtalo como posible duplicado
- No traduzcas ni reformules las preguntas ya revisadas sin decirlo
