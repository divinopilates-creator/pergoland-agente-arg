# agent/flujo.py - Envío de respuestas de Gian, avisos al CRM y pausa post-cierre
# Lo usan el webhook (main.py) y el scheduler de seguimientos (handoff.py).
import re
import logging
from agent.memory import obtener_historial
from agent.crm import (
    limpiar_tags_cliente, enviar_lead_crm, enviar_lead_distribuidor_crm,
    enviar_contacto_incompleto_crm,
)
from agent.handoff import pausar_contacto

logger = logging.getLogger("agentkit")

# Tags que cierran la conversación: después de enviarlos Gian se pausa en ese chat
TAGS_CIERRE = ("[LEAD:", "[LLAMADA:")

# Emojis y símbolos gráficos: nunca se envían al cliente
_EMOJIS = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U00002B00-\U00002BFF"
    "\U0000FE0F\U0000200D\U000020E3]"
)


def quitar_emojis(texto: str) -> str:
    limpio = _EMOJIS.sub("", texto)
    limpio = re.sub(r"[ \t]{2,}", " ", limpio)
    return "\n".join(linea.strip() for linea in limpio.split("\n")).strip()


def preparar_para_cliente(texto: str) -> str:
    """Texto final que ve el cliente: sin tags internos y sin emojis."""
    return quitar_emojis(limpiar_tags_cliente(texto or ""))


def tiene_tag_cierre(texto: str) -> bool:
    return any(tag in (texto or "") for tag in TAGS_CIERRE)


def _nombre_tag_llamada(texto: str) -> str:
    m = re.search(r"\[LLAMADA:[^\]]*nombre=([^|\]]*)", texto or "")
    return m.group(1).strip() if m else ""


async def despachar_respuesta(proveedor, telefono: str, nombre: str, respuesta: str,
                              avisar_incompleto: bool = True):
    """
    1. Envía al cliente la respuesta limpia (si queda texto).
    2. Avisa al CRM según el tag de ESTA respuesta.
    3. Si la respuesta cierra (LEAD o LLAMADA), pausa a Gian en el chat.
    La respuesta ya debe estar guardada en el historial (con tags) antes de llamar.
    """
    texto_cliente = preparar_para_cliente(respuesta)
    if texto_cliente:
        await proveedor.enviar_mensaje(telefono, texto_cliente)

    historial = await obtener_historial(telefono, limite=60)

    if "[LEAD:" in respuesta:
        await enviar_lead_crm(telefono, nombre, historial)
    elif "[LLAMADA:" in respuesta:
        await enviar_lead_crm(telefono, _nombre_tag_llamada(respuesta) or nombre, historial)
    elif "[LEAD_MADERA:" in respuesta:
        await enviar_lead_distribuidor_crm(telefono, historial)
    elif avisar_incompleto:
        await enviar_contacto_incompleto_crm(telefono, nombre, historial)

    if tiene_tag_cierre(respuesta):
        await pausar_contacto(telefono)
        logger.info(f"Cierre enviado a {telefono} - Gian pausado en este chat")

    return texto_cliente
