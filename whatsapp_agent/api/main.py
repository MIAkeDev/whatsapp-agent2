"""
API PRINCIPAL — FastAPI
"""
import json
import logging
import asyncio
import tempfile
import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

import redis.asyncio as aioredis
from fastapi import FastAPI, UploadFile, File, Form, WebSocket, WebSocketDisconnect, Depends, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, String

from config.settings import settings
from db.database import init_db, get_db
from db.models import Campana, Cliente, Envio, Respuesta, AlertaInteresado
from ingesta.normalizador import leer_excel
from agentes.emisor.agente_emisor import AgenteEmisor
from agentes.receptor.agente_receptor import procesar_respuesta_cliente

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


class GestorWebSocket:
    def __init__(self):
        self.conexiones: list[WebSocket] = []

    async def conectar(self, ws: WebSocket):
        await ws.accept()
        self.conexiones.append(ws)

    def desconectar(self, ws: WebSocket):
        if ws in self.conexiones:
            self.conexiones.remove(ws)

    async def broadcast(self, mensaje: dict):
        muertos = []
        for ws in self.conexiones:
            try:
                await ws.send_json(mensaje)
            except Exception:
                muertos.append(ws)
        for ws in muertos:
            self.desconectar(ws)


gestor_ws = GestorWebSocket()
agente_emisor = AgenteEmisor()


async def escuchar_alertas_redis():
    try:
        r = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
        pubsub = r.pubsub()
        await pubsub.subscribe("alertas:dashboard")
        async for mensaje in pubsub.listen():
            if mensaje["type"] == "message":
                datos = json.loads(mensaje["data"])
                await gestor_ws.broadcast(datos)
    except Exception as e:
        logger.error(f"Error listener Redis: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    asyncio.create_task(escuchar_alertas_redis())
    logger.info("✅ Agente WhatsApp Bancario iniciado")
    yield


app = FastAPI(title="WhatsApp Agent Bancario", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ── WEBHOOK META ──────────────────────────────────────────────

@app.get("/webhook")
async def verificar_webhook(request: Request):
    params = dict(request.query_params)
    if params.get("hub.mode") == "subscribe" and params.get("hub.verify_token") == settings.META_VERIFY_TOKEN:
        return PlainTextResponse(params.get("hub.challenge"))
    raise HTTPException(status_code=403, detail="Token inválido")


@app.post("/webhook")
async def recibir_mensaje(request: Request, db: AsyncSession = Depends(get_db)):
    try:
        body = await request.json()
        entry = body.get("entry", [{}])[0]
        changes = entry.get("changes", [{}])[0]
        messages = changes.get("value", {}).get("messages", [])

        for msg in messages:
            if msg.get("type") != "text":
                continue

            numero = f"+{msg['from']}"
            texto = msg["text"]["body"]

            # Verificar si está marcado como NO_INTERESADO
            r = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
            ya_rechazado = await r.exists(f"no_interesado:{numero}")
            await r.aclose()
            if ya_rechazado:
                continue

            # Buscar cliente en BD
            result = await db.execute(
                select(Cliente).where(
                    Cliente.numeros_e164.cast(String).contains(numero)
                )
            )
            cliente = result.scalar_one_or_none()

            # Registrar respuesta
            respuesta = Respuesta(
                cliente_id=cliente.id if cliente else None,
                numero_origen=numero,
                texto=texto,
                meta_message_id=msg.get("id", ""),
            )
            db.add(respuesta)
            await db.flush()
            respuesta_id = respuesta.id
            await db.commit()

            # Procesar con el agente en background
            asyncio.create_task(_procesar_respuesta(
                respuesta_id=respuesta_id,
                numero=numero,
                texto=texto,
                nombre=cliente.nombre if cliente else None,
                cliente_id=cliente.id if cliente else None,
            ))

    except Exception as e:
        logger.error(f"Error webhook: {e}")

    return {"status": "ok"}


async def _procesar_respuesta(respuesta_id, numero, texto, nombre, cliente_id):
    try:
        resultado = await procesar_respuesta_cliente(numero=numero, mensaje=texto, nombre=nombre, cliente_id=cliente_id)

        async with AsyncSessionLocal() as db:
            resp = await db.get(Respuesta, respuesta_id)
            if resp:
                resp.clasificacion = resultado["clasificacion"]
                resp.score_interes = resultado["score_interes"]
                resp.alerta_emitida = resultado["alerta_emitida"]

            if resultado["alerta_emitida"]:
                db.add(AlertaInteresado(
                    cliente_id=cliente_id,
                    numero=numero,
                    nombre=nombre,
                    ultimo_mensaje=texto,
                    score=resultado["score_interes"],
                    clasificacion=resultado["clasificacion"],
                ))
            await db.commit()
    except Exception as e:
        logger.error(f"Error procesando respuesta: {e}")

# Import necesario para _procesar_respuesta
from db.database import AsyncSessionLocal


# ── CAMPAÑA ───────────────────────────────────────────────────

@app.post("/campana/crear")
async def crear_campana(
    archivo: UploadFile = File(...),
    nombre_campana: str = Form(...),
    mensaje_personalizado: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db),
):
    # Guardar archivo temporal
    sufijo = ".xlsx" if archivo.filename.endswith(".xlsx") else ".xls"
    with tempfile.NamedTemporaryFile(delete=False, suffix=sufijo) as tmp:
        tmp.write(await archivo.read())
        tmp_path = tmp.name

    try:
        resultado = leer_excel(tmp_path)
    except ValueError as e:
        os.unlink(tmp_path)
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass

    # Crear campaña
    campana = Campana(
        nombre=nombre_campana,
        archivo_origen=archivo.filename,
        mensaje_template=mensaje_personalizado or settings.MENSAJE_DEFAULT,
        total_filas=resultado["total_filas"],
        total_numeros=resultado["total_numeros"],
        estado="enviando",
    )
    db.add(campana)
    await db.flush()

    # Guardar clientes y armar lista de envío
    clientes_envio = []
    for c in resultado["clientes"]:
        if not c.valido:
            continue
        cliente = Cliente(
            numero_original=c.numero_original,
            numeros_e164=c.numeros_e164,
            nombre=c.nombre,
            datos_extra=c.datos_extra,
            campana_id=campana.id,
        )
        db.add(cliente)
        await db.flush()
        clientes_envio.append({
            "cliente_id": cliente.id,
            "numeros_e164": c.numeros_e164,
            "nombre": c.nombre,
            "datos_extra": c.datos_extra,
        })

    campana_id = campana.id
    await db.commit()

    # Lanzar envío en background
    async def enviar():
        async def registrar(cliente_id, numero, estado, meta_id, error, mensaje_enviado):
            async with AsyncSessionLocal() as s:
                s.add(Envio(
                    campana_id=campana_id,
                    cliente_id=cliente_id,
                    numero_destino=numero,
                    mensaje_enviado=mensaje_enviado,
                    estado=estado,
                    meta_message_id=meta_id,
                    error_detalle=error,
                    enviado_en=datetime.utcnow() if estado == "enviado" else None,
                ))
                await s.commit()

        await agente_emisor.enviar_campana(clientes_envio, campana_id, registrar)

        async with AsyncSessionLocal() as s:
            c = await s.get(Campana, campana_id)
            if c:
                c.estado = "completada"
                c.completada_en = datetime.utcnow()
                await s.commit()

    asyncio.create_task(enviar())

    return {
        "campana_id": campana_id,
        "mensaje": f"Campaña '{nombre_campana}' iniciada",
        "total_filas": resultado["total_filas"],
        "total_numeros": resultado["total_numeros"],
        "columna_detectada": resultado["columna_telefono_detectada"],
        "errores_muestra": resultado["errores"][:5],
    }


# ── DASHBOARD ─────────────────────────────────────────────────

@app.get("/dashboard/stats")
async def stats(db: AsyncSession = Depends(get_db)):
    enviados = await db.scalar(select(func.count(Envio.id)).where(Envio.estado == "enviado")) or 0
    respondieron = await db.scalar(select(func.count(Respuesta.id))) or 0
    interesados = await db.scalar(select(func.count(Respuesta.id)).where(Respuesta.clasificacion == "INTERESADO")) or 0
    sin_ver = await db.scalar(select(func.count(AlertaInteresado.id)).where(AlertaInteresado.vista == False)) or 0
    tasa = round(respondieron / enviados * 100, 1) if enviados else 0
    return {"total_enviados": enviados, "total_respondieron": respondieron,
            "total_interesados": interesados, "alertas_sin_ver": sin_ver, "tasa_respuesta": tasa}


@app.get("/dashboard/alertas")
async def alertas(limit: int = 50, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(AlertaInteresado)
        .order_by(AlertaInteresado.score.desc(), AlertaInteresado.creada_en.desc())
        .limit(limit)
    )
    return [{"id": a.id, "numero": a.numero, "nombre": a.nombre, "ultimo_mensaje": a.ultimo_mensaje,
             "score": a.score, "clasificacion": a.clasificacion, "vista": a.vista,
             "creada_en": a.creada_en.isoformat()} for a in result.scalars()]


@app.get("/dashboard/respuestas")
async def respuestas(clasificacion: Optional[str] = None, score_minimo: float = 0,
                     limit: int = 100, db: AsyncSession = Depends(get_db)):
    query = select(Respuesta).where(Respuesta.score_interes >= score_minimo)
    if clasificacion:
        query = query.where(Respuesta.clasificacion == clasificacion)
    query = query.order_by(Respuesta.score_interes.desc(), Respuesta.recibida_en.desc()).limit(limit)
    result = await db.execute(query)
    return [{"numero": r.numero_origen, "texto": r.texto, "clasificacion": r.clasificacion,
             "score": r.score_interes, "recibida_en": r.recibida_en.isoformat()} for r in result.scalars()]


@app.patch("/dashboard/alertas/{alerta_id}/vista")
async def marcar_vista(alerta_id: int, db: AsyncSession = Depends(get_db)):
    a = await db.get(AlertaInteresado, alerta_id)
    if not a:
        raise HTTPException(status_code=404)
    a.vista = True
    return {"ok": True}


# ── WEBSOCKET ─────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket(websocket: WebSocket):
    await gestor_ws.conectar(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        gestor_ws.desconectar(websocket)
