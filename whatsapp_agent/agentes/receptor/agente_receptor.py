"""
AGENTE 2 — RECEPTOR / CLASIFICADOR (LangGraph + Groq)
------------------------------------------------------
Escucha respuestas de clientes, las clasifica con Groq (gratis)
y emite alertas al dashboard. Corre independiente del Emisor.
"""

import json
import logging
from pathlib import Path
from typing import TypedDict, Optional
from datetime import datetime

import redis.asyncio as aioredis
from groq import AsyncGroq
from langgraph.graph import StateGraph, END

from config.settings import settings

logger = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).parent / "classifier_prompt.txt"
CLASSIFIER_SYSTEM_PROMPT = PROMPT_PATH.read_text(encoding="utf-8")

groq_client = AsyncGroq(api_key=settings.GROQ_API_KEY)


# ── ESTADO DEL GRAFO ──────────────────────────────────────────

class EstadoMensaje(TypedDict):
    numero: str
    nombre: Optional[str]
    mensaje_cliente: str
    historial: list[dict]
    clasificacion: Optional[str]
    score_interes: Optional[float]
    razon_clasificacion: Optional[str]
    requiere_atencion: Optional[bool]
    alerta_emitida: bool
    timestamp: str
    cliente_id: Optional[int]


# ── NODOS ─────────────────────────────────────────────────────

async def nodo_cargar_historial(estado: EstadoMensaje) -> EstadoMensaje:
    try:
        r = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
        key = f"historial:{estado['numero']}"
        historial_raw = await r.lrange(key, -10, -1)
        historial = [json.loads(h) for h in historial_raw]
        nuevo = {
            "rol": "cliente",
            "texto": estado["mensaje_cliente"],
            "timestamp": estado["timestamp"]
        }
        await r.rpush(key, json.dumps(nuevo))
        await r.expire(key, 60 * 60 * 24 * 30)
        await r.aclose()
        return {**estado, "historial": historial}
    except Exception as e:
        logger.warning(f"Historial no disponible: {e}")
        return {**estado, "historial": []}


async def nodo_clasificar(estado: EstadoMensaje) -> EstadoMensaje:
    """Usa Groq (llama-3.1-8b-instant) para clasificar — gratis y muy rápido."""
    contexto = ""
    if estado["historial"]:
        msgs = "\n".join(
            f"[{h['timestamp']}] {h['texto']}"
            for h in estado["historial"][-5:]
        )
        contexto = f"\n\nHistorial previo:\n{msgs}"

    mensaje_usuario = (
        f"Analiza esta respuesta de un cliente bancario peruano.{contexto}\n\n"
        f"Mensaje actual: \"{estado['mensaje_cliente']}\""
    )

    try:
        respuesta = await groq_client.chat.completions.create(
            model="llama-3.1-8b-instant",  # Gratis en Groq
            messages=[
                {"role": "system", "content": CLASSIFIER_SYSTEM_PROMPT},
                {"role": "user", "content": mensaje_usuario},
            ],
            max_tokens=300,
            temperature=0.1,
        )

        raw = respuesta.choices[0].message.content.strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        datos = json.loads(raw)

        return {
            **estado,
            "clasificacion": datos.get("clasificacion", "CONSULTA_RANDOM"),
            "score_interes": float(datos.get("score_interes", 10)),
            "razon_clasificacion": datos.get("razon", ""),
            "requiere_atencion": datos.get("requiere_atencion_urgente", False),
        }
    except Exception as e:
        logger.error(f"Error clasificando: {e}")
        return {
            **estado,
            "clasificacion": "CONSULTA_RANDOM",
            "score_interes": 10.0,
            "razon_clasificacion": f"Error: {str(e)}",
            "requiere_atencion": False,
        }


async def nodo_emitir_alerta(estado: EstadoMensaje) -> EstadoMensaje:
    try:
        r = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
        alerta = {
            "tipo": "ALERTA_INTERESADO",
            "numero": estado["numero"],
            "nombre": estado.get("nombre"),
            "mensaje": estado["mensaje_cliente"],
            "clasificacion": estado["clasificacion"],
            "score": estado["score_interes"],
            "razon": estado["razon_clasificacion"],
            "requiere_atencion": estado["requiere_atencion"],
            "timestamp": estado["timestamp"],
            "cliente_id": estado.get("cliente_id"),
        }
        await r.publish("alertas:dashboard", json.dumps(alerta))
        await r.lpush("alertas:recientes", json.dumps(alerta))
        await r.ltrim("alertas:recientes", 0, 99)
        await r.aclose()
        logger.info(f"🔔 ALERTA: {estado['numero']} | score={estado['score_interes']}")
        return {**estado, "alerta_emitida": True}
    except Exception as e:
        logger.error(f"Error emitiendo alerta: {e}")
        return {**estado, "alerta_emitida": False}


async def nodo_solo_log(estado: EstadoMensaje) -> EstadoMensaje:
    logger.info(
        f"LOG: {estado['numero']} | {estado['clasificacion']} | "
        f"score={estado['score_interes']} | \"{estado['mensaje_cliente'][:60]}\""
    )
    return {**estado, "alerta_emitida": False}


async def nodo_marcar_no_interesado(estado: EstadoMensaje) -> EstadoMensaje:
    try:
        r = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
        await r.set(f"no_interesado:{estado['numero']}", "1", ex=60 * 60 * 24 * 90)
        await r.aclose()
    except Exception:
        pass
    return {**estado, "alerta_emitida": False}


# ── ROUTER ────────────────────────────────────────────────────

def router_clasificacion(estado: EstadoMensaje) -> str:
    clasificacion = estado.get("clasificacion", "CONSULTA_RANDOM")
    score = estado.get("score_interes", 0)
    urgente = estado.get("requiere_atencion", False)

    if clasificacion == "INTERESADO" and score >= settings.SCORE_ALERTA_MINIMO:
        return "emitir_alerta"
    if clasificacion == "DUDOSO":
        return "emitir_alerta"
    if clasificacion == "FUERA_DE_TEMA" and urgente:
        return "emitir_alerta"
    if clasificacion == "NO_INTERESADO":
        return "marcar_no_interesado"
    return "solo_log"


# ── CONSTRUIR GRAFO ───────────────────────────────────────────

def construir_grafo_receptor():
    grafo = StateGraph(EstadoMensaje)
    grafo.add_node("cargar_historial", nodo_cargar_historial)
    grafo.add_node("clasificar", nodo_clasificar)
    grafo.add_node("emitir_alerta", nodo_emitir_alerta)
    grafo.add_node("solo_log", nodo_solo_log)
    grafo.add_node("marcar_no_interesado", nodo_marcar_no_interesado)

    grafo.set_entry_point("cargar_historial")
    grafo.add_edge("cargar_historial", "clasificar")
    grafo.add_conditional_edges(
        "clasificar",
        router_clasificacion,
        {
            "emitir_alerta": "emitir_alerta",
            "solo_log": "solo_log",
            "marcar_no_interesado": "marcar_no_interesado",
        },
    )
    grafo.add_edge("emitir_alerta", END)
    grafo.add_edge("solo_log", END)
    grafo.add_edge("marcar_no_interesado", END)
    return grafo.compile()


grafo_receptor = construir_grafo_receptor()


async def procesar_respuesta_cliente(
    numero: str,
    mensaje: str,
    nombre: Optional[str] = None,
    cliente_id: Optional[int] = None,
) -> EstadoMensaje:
    estado_inicial = EstadoMensaje(
        numero=numero,
        nombre=nombre,
        mensaje_cliente=mensaje,
        historial=[],
        clasificacion=None,
        score_interes=None,
        razon_clasificacion=None,
        requiere_atencion=None,
        alerta_emitida=False,
        timestamp=datetime.utcnow().isoformat(),
        cliente_id=cliente_id,
    )
    return await grafo_receptor.ainvoke(estado_inicial)
