"""
MÓDULO DE INGESTA
-----------------
Lee cualquier Excel del banco, detecta columnas CELULAR o TELF,
extrae TODOS los números por celda (pueden haber 1, 2 o 3 separados por guión),
descarta fijos (menos de 9 dígitos o que no empiezan en 9),
deduplica y devuelve lista limpia en formato +51XXXXXXXXX.

Probado con los Excel reales de ILO:
  - ' - 985050724'                        → [+51985050724]
  - '973874235-949334445 - 949334445'     → [+51973874235, +51949334445]
  - '940862274-953732636 - 949188072'     → [+51940862274, +51953732636, +51949188072]
  - '961231898-949145321-999150333'       → [+51961231898, +51949145321, +51999150333]
  - '997877616-931229340-'               → [+51997877616, +51931229340]
"""

import re
import pandas as pd
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ClienteNormalizado:
    numero_original: str          # Celda tal cual estaba en el Excel
    numeros_e164: list[str]       # Lista de números válidos extraídos (+51XXXXXXXXX)
    nombre: Optional[str]
    datos_extra: dict
    valido: bool
    razon_invalido: Optional[str]


# Palabras clave que identifican columna de teléfono
COLS_TELEFONO = ["celular", "telf", "telefono", "teléfono", "movil",
                 "móvil", "phone", "cel", "tlf", "whatsapp"]

# Palabras clave que identifican columna de nombre
COLS_NOMBRE = ["nombre", "name", "cliente", "nombre_cliente", "titular"]

# Palabras que NO son datos reales (encabezados repetidos dentro del Excel)
VALORES_IGNORAR = {"celular", "telf", "telefono", "nombre", "nan", "none", ""}


def extraer_numeros_moviles(celda: str) -> list[str]:
    """
    Extrae todos los números móviles peruanos válidos de una celda.
    Un número móvil peruano tiene exactamente 9 dígitos y empieza en 9.
    Ignora números fijos (empiezan en 0, 1, etc.) y bloques < 9 dígitos.
    """
    if not celda or celda.strip().lower() in VALORES_IGNORAR:
        return []

    # Extraer todos los bloques de dígitos consecutivos
    bloques = re.findall(r'\d+', celda)
    numeros = []

    for bloque in bloques:
        # Número móvil peruano: exactamente 9 dígitos, empieza en 9
        if len(bloque) == 9 and bloque.startswith('9'):
            e164 = f"+51{bloque}"
            if e164 not in numeros:
                numeros.append(e164)
        # Con código de país 51 adelante: 11 dígitos empezando en 519
        elif len(bloque) == 11 and bloque.startswith('519'):
            e164 = f"+{bloque}"
            if e164 not in numeros:
                numeros.append(e164)

    return numeros


def detectar_columna(df: pd.DataFrame, palabras_clave: list[str]) -> Optional[str]:
    """Encuentra qué columna del Excel corresponde a teléfono o nombre."""
    for col in df.columns:
        col_lower = str(col).lower().strip()
        for palabra in palabras_clave:
            if palabra in col_lower:
                return col
    return None


def leer_excel(ruta_archivo: str) -> dict:
    """
    Lee cualquier Excel del banco y devuelve los clientes con sus números limpios.

    Retorna:
    {
        "clientes": [ClienteNormalizado, ...],
        "columna_telefono_detectada": str,
        "columna_nombre_detectada": str | None,
        "total_filas": int,
        "total_numeros": int,    # puede ser mayor que filas por celdas con múltiples números
        "filas_sin_numero": int,
        "errores": [str, ...]
    }
    """
    # Intentar leer detectando dónde están los headers reales
    for header_row in [0, 1, 2]:
        try:
            df = pd.read_excel(ruta_archivo, dtype=str, header=header_row)
            # Si las columnas tienen nombres reales (no Unnamed) es el header correcto
            cols_reales = [c for c in df.columns if "Unnamed" not in str(c)]
            if len(cols_reales) >= 1:
                break
        except Exception:
            continue
    else:
        raise ValueError("No se pudo leer el Excel correctamente.")

    df.columns = [str(c).strip() for c in df.columns]

    # Detectar columna de teléfono
    col_tel = detectar_columna(df, COLS_TELEFONO)
    if not col_tel:
        raise ValueError(
            "No se encontró columna de teléfonos. "
            "Asegúrate de que haya una columna llamada: CELULAR, TELF, TELEFONO, etc."
        )

    # Detectar columna de nombre (opcional)
    col_nombre = detectar_columna(df, COLS_NOMBRE)

    # Columnas extra para datos adicionales
    cols_ignorar = {col_tel}
    if col_nombre:
        cols_ignorar.add(col_nombre)
    cols_extra = [c for c in df.columns if c not in cols_ignorar]

    clientes = []
    errores = []
    numeros_globales = set()  # Para deduplicar entre filas
    total_numeros = 0
    filas_sin_numero = 0

    for _, fila in df.iterrows():
        raw = str(fila.get(col_tel, "")).strip()

        # Ignorar filas con encabezado repetido o vacías
        if raw.lower() in VALORES_IGNORAR:
            continue

        nombre = None
        if col_nombre:
            n = str(fila.get(col_nombre, "")).strip()
            if n.lower() not in VALORES_IGNORAR:
                nombre = n

        datos_extra = {
            col: str(fila.get(col, "")).strip()
            for col in cols_extra
            if str(fila.get(col, "")).strip().lower() not in VALORES_IGNORAR
        }

        # Extraer todos los números de la celda
        numeros = extraer_numeros_moviles(raw)

        # Deduplicar contra números ya vistos en otras filas
        numeros_nuevos = [n for n in numeros if n not in numeros_globales]
        for n in numeros_nuevos:
            numeros_globales.add(n)

        if not numeros_nuevos:
            if not numeros:
                filas_sin_numero += 1
                errores.append(f"Sin número válido: '{raw}'")
            else:
                errores.append(f"Duplicado ignorado: '{raw}'")

            clientes.append(ClienteNormalizado(
                numero_original=raw,
                numeros_e164=[],
                nombre=nombre,
                datos_extra=datos_extra,
                valido=False,
                razon_invalido="Sin número móvil válido o duplicado",
            ))
            continue

        total_numeros += len(numeros_nuevos)
        clientes.append(ClienteNormalizado(
            numero_original=raw,
            numeros_e164=numeros_nuevos,
            nombre=nombre,
            datos_extra=datos_extra,
            valido=True,
            razon_invalido=None,
        ))

    validos = [c for c in clientes if c.valido]
    invalidos = [c for c in clientes if not c.valido]

    return {
        "clientes": clientes,
        "columna_telefono_detectada": col_tel,
        "columna_nombre_detectada": col_nombre,
        "total_filas": len(clientes),
        "total_numeros": total_numeros,
        "filas_validas": len(validos),
        "filas_sin_numero": filas_sin_numero,
        "errores": errores[:20],  # máximo 20 errores en el reporte
    }
