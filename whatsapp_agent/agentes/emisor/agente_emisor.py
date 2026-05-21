"""
AGENTE 1 — EMISOR
-----------------
Envía mensajes masivos via Meta WhatsApp Business API.
Respeta rate limiting. No toma control del WhatsApp del analista —
solo envía el mensaje inicial de la campaña.
"""

import asyncio
import httpx
import logging
from typing import Optional
from datetime import datetime

from config.settings import settings

logger = logging.getLogger(__name__)

META_API_BASE = f"https://graph.facebook.com/{settings.META_API_VERSION}"


class MetaWhatsAppClient:
    def __init__(self):
        self.base_url = f"{META_API_BASE}/{settings.META_PHONE_NUMBER_ID}/messages"
        self.headers = {
            "Authorization": f"Bearer {settings.META_WHATSAPP_TOKEN}",
            "Content-Type": "application/json",
        }

    async def enviar_mensaje(self, numero: str, mensaje: str) -> tuple[Optional[str], Optional[str]]:
        """Devuelve (meta_message_id, error)"""
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": numero,
            "type": "text",
            "text": {"preview_url": False, "body": mensaje},
        }
        async with httpx.AsyncClient(timeout=30) as client:
            try:
                resp = await client.post(self.base_url, headers=self.headers, json=payload)
                data = resp.json()
                if resp.status_code == 200:
                    msg_id = data.get("messages", [{}])[0].get("id")
                    return msg_id, None
                else:
                    error = data.get("error", {}).get("message", str(data))
                    logger.error(f"Error Meta API {numero}: STATUS={resp.status_code} RESPUESTA={data}")
                    return None, error
            except Exception as e:
                return None, str(e)


class AgenteEmisor:
    def __init__(self):
        self.whatsapp = MetaWhatsAppClient()

    def construir_mensaje(self, nombre: Optional[str], datos_extra: dict) -> str:
        nombre_usar = nombre or "estimado cliente"
        try:
            return settings.MENSAJE_DEFAULT.format(nombre=nombre_usar, **datos_extra)
        except KeyError:
            return settings.MENSAJE_DEFAULT.format(nombre=nombre_usar)

    async def enviar_campana(self, clientes: list[dict], campana_id: int, callback=None) -> dict:
        """
        clientes: lista de dicts con numero_e164, nombre, datos_extra, cliente_id
        Cada cliente puede tener múltiples números (celdas con 2-3 números).
        """
        resultados = {"enviados": 0, "fallidos": 0}
        intervalo = 1.0 / settings.RATE_LIMIT_POR_SEGUNDO

        for cliente in clientes:
            mensaje = self.construir_mensaje(cliente.get("nombre"), cliente.get("datos_extra", {}))

            # Enviar a TODOS los números del cliente (puede tener 1, 2 o 3)
            for numero in cliente.get("numeros_e164", []):
                msg_id, error = await self.whatsapp.enviar_mensaje(numero, mensaje)
                estado = "enviado" if msg_id else "fallido"

                if callback:
                    await callback(
                        cliente_id=cliente["cliente_id"],
                        numero=numero,
                        estado=estado,
                        meta_id=msg_id,
                        error=error,
                        mensaje_enviado=mensaje,
                    )

                resultados["enviados" if msg_id else "fallidos"] += 1
                await asyncio.sleep(intervalo)

        logger.info(f"Campaña {campana_id}: {resultados['enviados']} enviados, {resultados['fallidos']} fallidos")
        return resultados
