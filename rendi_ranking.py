"""
Rendir - Ranking de tasas (plazo fijo, FCI money market, cuentas remuneradas y dólar)
======================================================================================

Fuentes:
- BCRA Régimen de Transparencia (CSV oficial, sin token):
  https://www.bcra.gob.ar/archivos/Pdfs/BCRAyVos/PFIJO.CSV
  Actualiza dos veces por día hábil (11hs y 19hs).

- CAFCI (API pública NO documentada oficialmente, sin token):
  Catálogo:  https://estadisticas.cafci.org.ar/consulta-de-fondos.json
  Snapshot:  https://api.pub.cafci.org.ar/pb_get  (requiere headers de browser)
  Actualiza una vez al día (~18hs).

- Dólar oficial general (dolarapi.com, API pública gratuita sin token,
  mantenida por un proyecto comunitario grande y estable).

- Dólar oficial por entidad (bancos y billeteras): API pública de
  comparadolar.ar (mismo autor de dolarapi.com), en vivo, sin token.
  Reemplaza el intento anterior con una API que dejó de funcionar al
  día siguiente de integrarla.

- Cuentas remuneradas de billeteras sin fuente oficial (Carrefour,
  Naranja X, Fiwind): comparatasas.ar (mismo autor), que mantiene un
  ranking en vivo con fecha de vigencia por tasa. Reemplaza el scraper
  anterior de notas de iprofesional.com, mucho más irregular.

IMPORTANTE:
No pude probar este script contra las fuentes reales porque el entorno
donde lo escribí no tiene salida a bcra.gob.ar, cafci.org.ar,
comparadolar.ar ni comparatasas.ar. Corré
`python rendi_ranking.py --debug` la primera vez: te va a imprimir las
columnas crudas de cada fuente para que ajustemos los nombres si el
formato no coincide con lo que documenté acá.

Uso:
    pip install requests pandas
    python rendi_ranking.py                # imprime el ranking top 10
    python rendi_ranking.py --debug         # además imprime columnas crudas
    python rendi_ranking.py --json salida.json   # guarda el resultado

Para correrlo 2-3 veces al día: agendalo con el Programador de tareas de
Windows (o cron si lo corrés en Linux/servidor) apuntando a este script.
"""

import argparse
import io
import json
import os
import sys
from datetime import datetime, timezone, timedelta

# Hora de Argentina fija (UTC-3, sin horario de verano) - se usa siempre,
# sin importar en qué huso horario esté corriendo el script (PC local o
# el servidor en la nube de GitHub Actions, que corre en UTC).
ARG_TZ = timezone(timedelta(hours=-3))


def ahora_argentina() -> datetime:
    return datetime.now(ARG_TZ)

import pandas as pd
import requests

HEADERS_BROWSER = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
}

BCRA_PFIJO_URL = "https://www.bcra.gob.ar/archivos/Pdfs/BCRAyVos/PFIJO.CSV"
BCRA_CAJA_AHORRO_URL = "https://www.bcra.gob.ar/archivos/Pdfs/BCRAyVos/CAJADEAHORROS.CSV"
CAFCI_CATALOGO_URL = "https://estadisticas.cafci.org.ar/consulta-de-fondos.json"
CAFCI_SNAPSHOT_URL = "https://api.pub.cafci.org.ar/pb_get"

# Dólar oficial general (API pública, mantenida por enzonotario, sin token,
# la misma que usan decenas de apps de dólar en Argentina - la fuente más
# estable que encontramos para esto).
DOLAR_OFICIAL_URL = "https://dolarapi.com/v1/dolares/oficial"

# NOTA: el desglose de dólar por banco SÍ tiene fuente ahora: la API
# pública de comparadolar.ar (mismo autor de dolarapi.com), que trae
# compra/venta en vivo por entidad (bancos y billeteras), sin token.
# Decisión (JO, 22/09/2026): reemplaza el intento anterior con una API
# comunitaria que murió al día siguiente de escribirla.
COMPARADOLAR_API_URL = "https://api.comparadolar.ar/usd"


# Cuentas remuneradas de billeteras SIN fuente oficial (ni BCRA ni CAFCI):
# comparatasas.ar (mismo autor de dolarapi.com/comparadolar.ar) mantiene un
# ranking en vivo que SÍ incluye Carrefour, Naranja X y Fiwind con fecha de
# vigencia. Decisión (JO, 22/09/2026): reemplaza el scraper de notas de
# iprofesional.com, que era mucho más frágil e irregular.
COMPARATASAS_URL = "https://comparatasas.ar/"
CUENTAS_REMUNERADAS_ALIAS = {
    "Mi Carrefour": ["carrefour banco", "carrefour"],
    "Naranja X": ["naranja x"],
    "Fiwind": ["fiwind"],
}


# Fondos Money Market que usan las billeteras (ajustar/completar nombres
# tal como aparecen en el catálogo de CAFCI una vez que lo inspeccionemos).
FONDOS_BILLETERAS = {
    "Cocos": ["cocos", "cocoscap"],
    "Mercado Pago": ["mercado fondo", "mercado pago"],
    "Personal Pay": ["personal pay", "consultatio personal", "delta pesos"],
    "Ualá": ["uala", "ualintec"],  # FCI propio: "FCI Ualintec Ahorro Pesos"
    "Naranja X": ["naranja x"],  # ídem
    "Mi Carrefour": ["carrefour", "bind"],  # cuenta remunerada, no FCI - ídem
    "Lemon Cash": ["lemon", "vinci compass liquidez"],
    "Claro Pay": ["claro pay", "sbs ahorro pesos"],  # confirmado: invierte en "SBS Ahorro Pesos FCI" (SBS Asset Management)
    # Prex, Astropay, IEB+, N1U y LB Finanzas: decisión (JO, 27/09/2026)
    # quedan afuera del ranking definitivamente, no se busca más su fondo.
}

# Bancos que nos interesa priorizar si aparecen en el top (además de tomar
# el resto por orden de tasa)
# Bancos masivos/conocidos de Argentina - filtro para el ranking de
# plazo fijo (excluye bancos mayoristas/regionales chicos). Se matchea
# por substring contra "DESCRIPCIÓN DE ENTIDAD" en mayúsculas, así que
# alcanza con una palabra distintiva de cada uno.
BANCOS_MASIVOS = [
    "NACION",
    "PROVINCIA DE BUENOS AIRES",
    "SANTANDER",
    "BBVA",
    "GALICIA",
    "MACRO",
    "CREDICOOP",
    "CIUDAD",  # Banco Ciudad de Buenos Aires
    "PATAGONIA",
    "SUPERVIELLE",
    "ICBC",
    "ITAU",
    "COMAFI",
    "HIPOTECARIO",
    "BRUBANK",
    "HSBC",
    # OJO: "Mi Carrefour" (Banco de Servicios Financieros) NO es plazo
    # fijo, es cuenta remunerada - no va a aparecer nunca acá. Va en
    # FONDOS_BILLETERAS/pendiente de scraping, no en esta lista.
]

TOP_N = 10

# Algunos bancos operan con una razón social distinta a como los conoce
# el público (ej: Banco Carrefour = "Banco de Servicios Financieros S.A."
# ante el BCRA). Mapeo para mostrar el nombre real de marca.
NOMBRES_DISPLAY = {
    "BANCO DE SERVICIOS FINANCIEROS S.A.": "Banco Carrefour",
}


def obtener_plazo_fijo(debug: bool = False) -> pd.DataFrame:
    """Descarga y parsea el CSV de plazo fijo del BCRA."""
    resp = requests.get(BCRA_PFIJO_URL, headers=HEADERS_BROWSER, timeout=20)
    resp.raise_for_status()

    # El BCRA suele publicar estos CSV en latin-1 y separados por ';'.
    # Si el formato real difiere, --debug te va a mostrar las primeras
    # líneas crudas para ajustar esto.
    raw_text = resp.content.decode("latin-1", errors="replace")

    if debug:
        print("\n--- PFIJO.CSV (primeras 15 líneas crudas) ---")
        for line in raw_text.splitlines()[:15]:
            print(line)
        print("--- fin muestra ---\n")

    df = pd.read_csv(io.StringIO(raw_text), sep=";", engine="python")
    df.columns = [c.strip().upper() for c in df.columns]

    if debug:
        print("Columnas detectadas en PFIJO.CSV:", list(df.columns))

    return df


def obtener_caja_ahorro(debug: bool = False) -> pd.DataFrame:
    """Descarga y parsea el CSV de cajas de ahorro del BCRA (mismo régimen
    que el de plazo fijo). Acá es donde deberían aparecer las cuentas
    digitales remuneradas de billeteras que operan como entidad financiera
    (ej: "Mi Carrefour"), si es que las declaran bajo esta categoría.

    Todavía no confirmamos la estructura exacta de columnas - --debug
    imprime las primeras líneas crudas para ajustar el parseo.
    """
    resp = requests.get(BCRA_CAJA_AHORRO_URL, headers=HEADERS_BROWSER, timeout=20)
    resp.raise_for_status()

    raw_text = resp.content.decode("latin-1", errors="replace")

    if debug:
        print("\n--- CAJADEAHORROS.CSV (primeras 15 líneas crudas) ---")
        for line in raw_text.splitlines()[:15]:
            print(line)
        print("--- fin muestra ---\n")

    df = pd.read_csv(io.StringIO(raw_text), sep=";", engine="python")
    df.columns = [c.strip().upper() for c in df.columns]

    if debug:
        print("Columnas detectadas en CAJADEAHORROS.CSV:", list(df.columns))
        col_entidad_probable = next((c for c in df.columns if "ENTIDAD" in c and "CÓDIGO" not in c), None)
        if col_entidad_probable:
            posible = df[
                df[col_entidad_probable].astype(str).str.upper().str.contains(
                    "CARREFOUR|SERVICIOS FINANCIEROS|NARANJA", regex=True, na=False
                )
            ]
            print("\n--- Búsqueda de Carrefour/Naranja X en CAJADEAHORROS.CSV ---")
            if posible.empty:
                print("No aparece ninguna fila con esos nombres en este CSV tampoco.")
            else:
                print(posible.to_string())
            print("--- fin búsqueda ---\n")

    return df


def obtener_fci_money_market(debug: bool = False) -> pd.DataFrame:
    """Descarga el snapshot diario de CAFCI y devuelve los fondos de la
    categoría "Mercado de Dinero", con su variación mensual de VCP
    (más estable que la variación de un solo día para estimar la TNA).

    Estructura real del Excel (confirmada 19/09/2026):
    - Fila 7 (índice 0 del archivo): encabezado principal.
    - Fila 8: sub-encabezado. La columna 9 ("Variacion cuotaparte %")
      trae como sub-título la fecha de referencia del mes anterior
      (ej: "31/08/26") - es el % de variación de la cuotaparte desde
      esa fecha hasta la fecha del dato (columna 4, "Fecha").
    - A partir de la fila 9: filas de datos, con filas-título de
      categoría intercaladas (columna 0 con texto, columna 4 vacía).
    - Usamos la variación MENSUAL (columna 9) anualizada según los
      días reales transcurridos, en vez de la variación DIARIA
      (columna 7) que es demasiado ruidosa para estimar una TNA.
    """
    resp = requests.get(CAFCI_SNAPSHOT_URL, headers=HEADERS_BROWSER, timeout=30)
    resp.raise_for_status()

    crudo_df = pd.read_excel(io.BytesIO(resp.content), header=None)

    if debug:
        print("\n--- Primeras 14 filas crudas del snapshot CAFCI (sin header) ---")
        print(crudo_df.head(14).to_string())
        print("--- fin muestra ---\n")

    COL_FONDO = 0
    COL_FECHA = 4
    COL_VARIAC_DIARIA = 7
    COL_VARIAC_MENSUAL = 9

    # La fecha de referencia del "mes anterior" está en la fila 8 (sub-header),
    # como texto dentro de la columna 9 (ej: "31/08/26").
    fecha_ref_mensual = None
    try:
        fecha_ref_str = str(crudo_df.iloc[8][COL_VARIAC_MENSUAL]).strip()
        fecha_ref_mensual = datetime.strptime(fecha_ref_str, "%d/%m/%y")
    except (ValueError, TypeError, IndexError):
        pass

    if debug:
        print(f"Fecha de referencia para variación mensual: {fecha_ref_mensual}")

    filas_fondos = []
    categoria_actual = None
    categorias_vistas = set()

    for i in range(9, len(crudo_df)):
        fila = crudo_df.iloc[i]
        nombre_col0 = fila[COL_FONDO]

        if pd.isna(nombre_col0):
            continue  # fila vacía, se ignora

        tiene_fecha = pd.notna(fila[COL_FECHA])

        if not tiene_fecha:
            # Fila-título de categoría (ej: "Mercado de Dinero Peso Argentina")
            categoria_actual = str(nombre_col0).strip()
            categorias_vistas.add(categoria_actual)
            continue

        # Fila de datos de un fondo
        try:
            variac_diaria = float(fila[COL_VARIAC_DIARIA])
        except (ValueError, TypeError):
            variac_diaria = float("nan")

        try:
            variac_mensual = float(fila[COL_VARIAC_MENSUAL])
        except (ValueError, TypeError):
            variac_mensual = float("nan")

        tna_mensual = float("nan")
        if fecha_ref_mensual is not None and not pd.isna(variac_mensual):
            try:
                fecha_dato = datetime.strptime(str(fila[COL_FECHA]).strip(), "%d/%m/%y")
                dias = (fecha_dato - fecha_ref_mensual).days
                if dias > 0:
                    tna_mensual = variac_mensual * (365 / dias)
            except (ValueError, TypeError):
                pass

        filas_fondos.append({
            "fondo": str(nombre_col0).strip(),
            "categoria": categoria_actual or "",
            "variac_diaria_pct": variac_diaria,
            "variac_mensual_pct": variac_mensual,
            "tna_estimada": tna_mensual,
        })

    if debug:
        print(f"Categorías encontradas en el snapshot: {sorted(categorias_vistas)}")

    fci_df = pd.DataFrame(filas_fondos)

    if debug and not fci_df.empty:
        mm = fci_df[fci_df["categoria"].str.contains("mercado de dinero", case=False, na=False)]
        print(f"\nFondos bajo 'Mercado de Dinero' encontrados: {len(mm)}")
        print(mm.head(20).to_string())

        # Volcamos el listado completo a un CSV para revisarlo en Excel
        # y encontrar ahí los nombres reales de los fondos que administran
        # las billeteras que todavía no matchean.
        try:
            mm_ordenado = mm.sort_values("fondo")
            mm_ordenado.to_csv("fci_mercado_dinero_debug.csv", index=False, sep=";", encoding="utf-8-sig")
            print(f"\nListado completo de {len(mm_ordenado)} fondos 'Mercado de Dinero' "
                  "guardado en fci_mercado_dinero_debug.csv (misma carpeta del script) "
                  "para revisar en Excel.")
        except Exception as e:
            print(f"No se pudo guardar el CSV de debug: {e}", file=sys.stderr)

    return fci_df


def obtener_dolar_oficial(debug: bool = False) -> dict:
    """Cotización general del dólar oficial (compra/venta), vía dolarapi.com."""
    resp = requests.get(DOLAR_OFICIAL_URL, headers=HEADERS_BROWSER, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    if debug:
        print("\n--- Respuesta cruda dolarapi.com (oficial) ---")
        print(data)
        print("--- fin muestra ---\n")

    return {
        "compra": data.get("compra"),
        "venta": data.get("venta"),
        "fecha": data.get("fechaActualizacion"),
    }


def obtener_dolar_entidades(debug: bool = False) -> list:
    """Cotizacion de venta del dolar oficial por entidad (bancos y
    billeteras), en vivo, via la API publica de comparadolar.ar (mismo
    autor de dolarapi.com). Se queda solo con las entidades marcadas como
    "Oficial" (no MEP/CCL/Cripto). No requiere token.

    El esquema exacto de la respuesta no esta 100% confirmado sin poder
    probarlo en vivo - correr con --debug muestra una entrada cruda de
    ejemplo para ajustar los nombres de campo si hiciera falta.
    """
    resultado = []
    try:
        resp = requests.get(COMPARADOLAR_API_URL, headers=HEADERS_BROWSER, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"AVISO: no se pudo obtener el dolar por entidad de comparadolar.ar: {e}", file=sys.stderr)
        return resultado

    if isinstance(data, list):
        entradas = data
    elif isinstance(data, dict):
        entradas = data.get("results") or data.get("data") or data.get("quotes") or []
    else:
        entradas = []

    if debug:
        print(f"\n--- comparadolar.ar API: {len(entradas)} entradas recibidas ---")
        if entradas:
            print("Ejemplo de una entrada cruda:", entradas[0])
        print("--- fin muestra ---\n")

    for entrada in entradas:
        if not isinstance(entrada, dict):
            continue
        tipo = str(entrada.get("tipo") or entrada.get("type") or entrada.get("category") or "").lower()
        if tipo and "oficial" not in tipo:
            continue  # nos interesa solo el segmento "Oficial", no MEP/CCL/Cripto
        nombre = entrada.get("prettyName") or entrada.get("nombre") or entrada.get("name") or entrada.get("entity")
        venta = entrada.get("venta") or entrada.get("sell") or entrada.get("ask")
        if nombre is None or venta is None:
            continue
        try:
            resultado.append({"nombre": str(nombre), "venta": float(venta)})
        except (TypeError, ValueError):
            continue

    resultado.sort(key=lambda x: x["venta"])

    if debug:
        print(f"Entidades 'Oficial' extraidas: {len(resultado)}")
        for r in resultado[:15]:
            print(f"  {r['nombre']:<25} venta ${r['venta']}")

    return resultado[:10]


def obtener_cuentas_remuneradas_comparatasas(debug: bool = False) -> list:
    """Cuentas remuneradas y billeteras (Carrefour, Naranja X, Fiwind) via
    comparatasas.ar, que mantiene un ranking en vivo con fecha de vigencia
    para cada tasa. Reemplaza el scraper anterior de notas de
    iprofesional.com, mucho mas irregular.
    """
    import re

    registros = []
    try:
        resp = requests.get(COMPARATASAS_URL, headers=HEADERS_BROWSER, timeout=20)
        resp.raise_for_status()
        texto = resp.text
    except Exception as e:
        print(f"AVISO: no se pudo acceder a comparatasas.ar: {e}", file=sys.stderr)
        return registros

    texto_plano = re.sub(r"<[^>]+>", " ", texto)

    if debug:
        print(f"\n--- comparatasas.ar: status {resp.status_code}, "
              f"{len(texto)} caracteres crudos, {len(texto_plano)} de texto plano ---")
        if len(texto_plano.strip()) < 200:
            print("Respuesta sospechosamente corta, esto es todo lo que llegó:")
            print(repr(texto))
            print("Headers de la respuesta:", dict(resp.headers))

    for nombre, alias in CUENTAS_REMUNERADAS_ALIAS.items():
        encontrado = False
        for alias_texto in alias:
            m = re.search(
                rf"{re.escape(alias_texto)}.{{0,300}}?(\d{{1,2}}[,.]?\d{{0,2}})\s*%\s*TNA",
                texto_plano, re.IGNORECASE | re.DOTALL,
            )
            if m:
                tna = float(m.group(1).replace(",", "."))
                resto = texto_plano[m.end():m.end() + 60]
                fecha_match = re.search(r"vigente desde el (\d{2}/\d{2}/\d{4})", resto)
                fecha = fecha_match.group(1) if fecha_match else None
                producto = "Cuenta remunerada (via comparatasas.ar"
                producto += f", vigente desde {fecha})" if fecha else ")"
                registros.append({
                    "nombre": nombre,
                    "producto": producto,
                    "tna": tna,
                    "tipo": "billetera",
                })
                encontrado = True
                break
        if not encontrado and debug:
            print(f"  NO encontrado en comparatasas.ar: {nombre} (alias probados: {alias})")

    if debug:
        print(f"\nCuentas remuneradas extraidas de comparatasas.ar: {registros}\n")

    return registros


def armar_ranking(plazo_fijo_df: pd.DataFrame, fci_df: pd.DataFrame,
                   cuentas_remuneradas: list = None, debug: bool = False) -> list:
    """Combina ambas fuentes y devuelve el top 10 por tasa descendente.

    Columnas reales confirmadas en PFIJO.CSV (BCRA, 18/09/2026):
    'CÓDIGO DE ENTIDAD', 'DESCRIPCIÓN DE ENTIDAD', 'FECHA DE INFORMACIÓN',
    'NOMBRE COMPLETO DEL PLAZO FIJO', 'NOMBRE CORTO DEL PLAZO FIJO',
    'DENOMINACIÓN', 'MONTO MÍNIMO A INVERTIR', 'PLAZO MÍNIMO A INVERTIR',
    'CANAL DE CONSTITUCIÓN', 'TASA EFECTIVA ANUAL MÍNIMA',
    'TERRITORIO DE VALIDEZ DE LA OFERTA', 'MÁS INFORMACIÓN'

    Ojo: es TASA EFECTIVA ANUAL (TEA), no TNA. Son parecidas pero no
    idénticas - lo dejamos etiquetado como TEA en el resultado para no
    mezclar unidades con la TNA de los FCI sin avisar.
    """
    registros = []
    if cuentas_remuneradas:
        registros.extend(cuentas_remuneradas)

    # --- Plazo fijo ---
    col_entidad = "DESCRIPCIÓN DE ENTIDAD"
    col_tasa = "TASA EFECTIVA ANUAL MÍNIMA"
    col_denominacion = "DENOMINACIÓN"
    col_plazo = "PLAZO MÍNIMO A INVERTIR"

    faltan = [c for c in (col_entidad, col_tasa, col_denominacion, col_plazo)
              if c not in plazo_fijo_df.columns]

    if debug and col_entidad in plazo_fijo_df.columns:
        posible_carrefour = plazo_fijo_df[
            plazo_fijo_df[col_entidad].astype(str).str.upper().str.contains(
                "CARREFOUR|SERVICIOS FINANCIEROS", regex=True, na=False
            )
        ]
        print("\n--- Búsqueda de Carrefour en el CSV crudo (sin filtros de pesos/30 días) ---")
        if posible_carrefour.empty:
            print("No aparece ninguna fila con 'CARREFOUR' ni 'SERVICIOS FINANCIEROS' "
                  "en el padrón del BCRA. Puede que no esté adherida a este régimen, "
                  "o que use otra razón social todavía no identificada.")
        else:
            print(posible_carrefour[[col_entidad, col_denominacion, col_plazo, col_tasa]].to_string())
        print("--- fin búsqueda ---\n")

    if not faltan:
        df = plazo_fijo_df.copy()
        # Solo depósitos en pesos, a 30 días (estándar para comparar bancos)
        df = df[df[col_denominacion].astype(str).str.strip().str.lower() == "pesos"]
        df = df[df[col_plazo].astype(str).str.strip().str.lower() == "30 días"]

        df["_tasa_num"] = (
            df[col_tasa].astype(str).str.replace(",", ".", regex=False)
        )
        df["_tasa_num"] = pd.to_numeric(df["_tasa_num"], errors="coerce")
        df = df.dropna(subset=["_tasa_num"])

        # Un banco puede reportar varias tasas (distintos canales/montos):
        # nos quedamos con la mejor tasa ofrecida por cada uno.
        mejores = df.loc[df.groupby(col_entidad)["_tasa_num"].idxmax()]

        # El padrón de "entidades" del BCRA incluye mutuales, cooperativas
        # de crédito chicas, etc. que nadie compara contra una billetera.
        # Nos quedamos solo con las que arrancan con "BANCO"...
        mejores = mejores[
            mejores[col_entidad].astype(str).str.strip().str.upper().str.startswith("BANCO")
        ]

        # ...y de esos, solo los bancos masivos/conocidos (decisión de
        # producto: no tiene sentido comparar contra bancos mayoristas
        # chicos que nadie usa desde el celular). Lista a mano, ajustar
        # si falta o sobra alguno.
        mejores = mejores[
            mejores[col_entidad].astype(str).str.upper().apply(
                lambda nombre: any(b in nombre for b in BANCOS_MASIVOS)
            )
        ]

        if debug:
            print("\n--- Todos los bancos masivos encontrados (antes del corte top 10) ---")
            print(
                mejores[[col_entidad, "_tasa_num"]]
                .sort_values("_tasa_num", ascending=False)
                .to_string(index=False)
            )
            print("--- fin listado ---\n")

        for _, fila in mejores.iterrows():
            nombre_real = str(fila[col_entidad]).strip().upper()
            nombre_mostrar = NOMBRES_DISPLAY.get(nombre_real, str(fila[col_entidad]).strip().title())
            registros.append({
                "nombre": nombre_mostrar,
                "producto": "Plazo fijo 30 días (TEA)",
                "tna": float(fila["_tasa_num"]),
                "tipo": "banco",
            })
    else:
        print(f"AVISO: faltan columnas esperadas en PFIJO.CSV: {faltan}. "
              "Correr con --debug para revisar.", file=sys.stderr)

    # --- FCI money market ---
    if not fci_df.empty and "categoria" in fci_df.columns:
        mm_df = fci_df[
            fci_df["categoria"].str.contains("mercado de dinero", case=False, na=False)
        ]
        # Cada billetera puede tener varias clases del mismo fondo
        # (Clase A, B, C...) - juntamos todas las TNA válidas y nos
        # quedamos con la mejor por billetera, para no duplicar filas.
        candidatos_por_billetera = {}
        for _, fila in mm_df.iterrows():
            nombre_fondo = str(fila["fondo"]).lower()
            for billetera, alias in FONDOS_BILLETERAS.items():
                if any(a in nombre_fondo for a in alias):
                    tna_estimada = fila["tna_estimada"]
                    if pd.isna(tna_estimada) or tna_estimada <= 0:
                        break  # dato faltante o variación negativa/nula, se descarta
                    candidatos_por_billetera.setdefault(billetera, []).append(tna_estimada)
                    break

        for billetera, tnas in candidatos_por_billetera.items():
            registros.append({
                "nombre": billetera,
                "producto": "FCI Money Market (TNA estimada, base mensual)",
                "tna": max(tnas),
                "tipo": "billetera",
            })

        if debug:
            print("\n--- Diagnóstico de matching por billetera ---")
            for billetera, alias in FONDOS_BILLETERAS.items():
                fondos_matcheados = mm_df[
                    mm_df["fondo"].str.lower().apply(lambda n: any(a in n for a in alias))
                ]
                if billetera in candidatos_por_billetera:
                    print(f"  OK  {billetera}: {len(fondos_matcheados)} clase(s) encontrada(s), "
                          f"mejor TNA estimada {max(candidatos_por_billetera[billetera]):.2f}%")
                elif len(fondos_matcheados) > 0:
                    print(f"  --  {billetera}: matcheó {len(fondos_matcheados)} fondo(s) pero "
                          "sin variación mensual válida (se descartó):")
                    print(fondos_matcheados[["fondo", "variac_mensual_pct", "tna_estimada"]].to_string())
                else:
                    print(f"  NO  {billetera}: ningún fondo bajo 'Mercado de Dinero' coincide "
                          f"con los alias {alias} - revisar nombre real del fondo")
            print("--- fin diagnóstico ---\n")
    else:
        print("AVISO: no se pudo armar la tabla de fondos CAFCI. "
              "Correr con --debug para revisar.", file=sys.stderr)

    # Rankings separados (pedido explícito: billeteras y plazo fijo no se
    # mezclan en una sola lista) - cada uno ordenado de mayor a menor tasa.
    billeteras = sorted(
        [r for r in registros if r["tipo"] == "billetera"],
        key=lambda r: -r["tna"],
    )
    bancos = sorted(
        [r for r in registros if r["tipo"] == "banco"],
        key=lambda r: -r["tna"],
    )[:TOP_N]

    return {"billeteras": billeteras, "bancos": bancos}


def main():
    parser = argparse.ArgumentParser(description="Ranking de tasas Rendir")
    parser.add_argument("--debug", action="store_true",
                         help="Imprime la estructura cruda de cada fuente")
    parser.add_argument("--json", metavar="ARCHIVO",
                         help="Guarda el resultado en un archivo JSON")
    args = parser.parse_args()

    print(f"Consultando fuentes... ({ahora_argentina().strftime('%Y-%m-%d %H:%M')})")

    try:
        plazo_fijo_df = obtener_plazo_fijo(debug=args.debug)
    except Exception as e:
        print(f"ERROR al obtener plazo fijo del BCRA: {e}", file=sys.stderr)
        plazo_fijo_df = pd.DataFrame()

    try:
        obtener_caja_ahorro(debug=args.debug)  # solo diagnóstico por ahora
    except Exception as e:
        print(f"ERROR al obtener cajas de ahorro del BCRA: {e}", file=sys.stderr)

    try:
        fci_df = obtener_fci_money_market(debug=args.debug)
    except Exception as e:
        print(f"ERROR al obtener FCI de CAFCI: {e}", file=sys.stderr)
        fci_df = pd.DataFrame()

    try:
        cuentas_remuneradas = obtener_cuentas_remuneradas_comparatasas(debug=args.debug)
    except Exception as e:
        print(f"ERROR al obtener cuentas remuneradas de comparatasas.ar: {e}", file=sys.stderr)
        cuentas_remuneradas = []

    try:
        dolar_oficial = obtener_dolar_oficial(debug=args.debug)
    except Exception as e:
        print(f"ERROR al obtener el dólar oficial: {e}", file=sys.stderr)
        dolar_oficial = {}

    try:
        dolar_entidades = obtener_dolar_entidades(debug=args.debug)
    except Exception as e:
        print(f"ERROR al obtener el dólar por entidad de comparadolar.ar: {e}", file=sys.stderr)
        dolar_entidades = []

    resultado = armar_ranking(plazo_fijo_df, fci_df, cuentas_remuneradas, debug=args.debug)

    print("\n=== DÓLAR OFICIAL (venta) ===")
    if dolar_oficial:
        print(f"${dolar_oficial.get('venta')}   (actualizado: {dolar_oficial.get('fecha')})")
    else:
        print("(sin datos - revisar diagnóstico con --debug)")

    if dolar_entidades:
        print("\n=== DÓLAR OFICIAL POR ENTIDAD (venta, de menor a mayor) ===")
        for i, r in enumerate(dolar_entidades, 1):
            print(f"{i:02d}. {r['nombre']:<25} ${r['venta']}")

    print("\n=== RANKING BILLETERAS (FCI Money Market + cuentas remuneradas) ===")
    if resultado["billeteras"]:
        for i, r in enumerate(resultado["billeteras"], 1):
            print(f"{i:02d}. {r['nombre']:<25} {r['producto']:<35} {r['tna']:.2f}%")
    else:
        print("(sin resultados - revisar diagnóstico con --debug)")

    print("\n=== RANKING PLAZO FIJO BANCOS (top 10) ===")
    for i, r in enumerate(resultado["bancos"], 1):
        print(f"{i:02d}. {r['nombre']:<25} {r['producto']:<35} {r['tna']:.2f}%")

    if args.json:
        salida = {
            "actualizado": ahora_argentina().isoformat(),
            "billeteras": resultado["billeteras"],
            "bancos": resultado["bancos"],
            "dolar_oficial": dolar_oficial,
            "dolar_entidades": dolar_entidades,
        }
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(salida, f, ensure_ascii=False, indent=2)
        print(f"\nGuardado en {args.json}")

    # Archivo que lee el mockup (mockup.html) para mostrar datos reales en
    # vez de los de ejemplo. Se escribe SIEMPRE (no depende de --json), al
    # lado del script - mantené mockup.html en la misma carpeta para que
    # lo encuentre. No hace falta ningún servidor: mockup.html lo carga
    # con un <script src="rendi_data.js">, así que sirve abrir el archivo
    # directamente con doble clic.
    mockup_data = {
        "actualizado": ahora_argentina().strftime("%d/%m/%Y %H:%M"),
        "billeteras": resultado["billeteras"],
        "bancos": resultado["bancos"],
        "dolar_oficial": dolar_oficial,
        "dolar_entidades": dolar_entidades,
    }
    ruta_js = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rendi_data.js")
    with open(ruta_js, "w", encoding="utf-8") as f:
        f.write("// Generado automáticamente por rendi_ranking.py - no editar a mano.\n")
        f.write("window.RENDI_DATA = ")
        json.dump(mockup_data, f, ensure_ascii=False, indent=2)
        f.write(";\n")
    print(f"Datos para el mockup guardados en {ruta_js}")


if __name__ == "__main__":
    main()
