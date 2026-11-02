# agent/handoff.py - Handoff humano y recordatorios automáticos
# Timer se activa desde el CRM al cambiar etapa, NO desde stop gian

import asyncio
import logging
import re
from datetime import datetime, timedelta
from sqlalchemy import String, DateTime, Integer, select, delete, text
from sqlalchemy.orm import Mapped, mapped_column
from agent.memory import Base, engine, async_session, guardar_mensaje

logger = logging.getLogger("agentkit")

# ── Mensajes automáticos ────────────────────────────────────────────
MSG_COTIZACION = (
    "Hola, soy Gian de Pergoland Argentina. "
    "Quería saber si pudiste revisar la cotización que te enviamos "
    "y si tenés alguna consulta sobre tu proyecto."
)

MSG_VISITA = (
    "Hola, soy Gian de Pergoland Argentina. "
    "Quería hacer un seguimiento después de la visita técnica. "
    "¿Pudiste revisar la propuesta con Martín?"
)

# ── Seguimientos por inactividad (flujo de calificación) ─────────────
# Horas desde el último mensaje real del cliente -> instrucción para Gian
SEGUIMIENTOS = [
    (24, "[SEGUIMIENTO:1]"),
    (48, "[SEGUIMIENTO:2]"),
    (72, "[CIERRE_SIN_RESPUESTA]"),
]
TOKENS_SISTEMA = ("[SEGUIMIENTO:", "[CIERRE_SIN_RESPUESTA]")
# Tags que indican que el flujo ya terminó: no se hace seguimiento
TAGS_FLUJO_TERMINADO = ("[LEAD:", "[LLAMADA:", "[LEAD_MADERA:")


async def _guardar_si_no_esta(telefono: str, texto: str):
    """Guarda el recordatorio en el historial (una sola vez) para reconocer su eco."""
    from agent.memory import obtener_historial
    ultimos = await obtener_historial(telefono, limite=1)
    if not ultimos or ultimos[-1]["content"] != texto:
        await guardar_mensaje(telefono, "assistant", texto)


# ── Modelo de base de datos ──────────────────────────────────────────
class HandoffEstado(Base):
    """Estado de pausa y timer por contacto."""
    __tablename__ = "handoff_estado"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telefono: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    pausado_en: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    tipo_timer: Mapped[str] = mapped_column(String(20), default="stop")
    timer_activado_en: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    recordatorio_enviado: Mapped[str] = mapped_column(String(10), default="pendiente")


async def inicializar_handoff_db():
    """Crea la tabla handoff_estado si no existe."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Tabla handoff_estado inicializada")

async def pausar_contacto(telefono: str):
    """Pausa a Gian SIN activar timer. El timer se activa desde el CRM."""
    async with async_session() as session:
        result = await session.execute(
            select(HandoffEstado).where(HandoffEstado.telefono == telefono)
        )
        existente = result.scalar_one_or_none()

        if existente:
            existente.pausado_en = datetime.utcnow()
            existente.tipo_timer = "stop"
            existente.timer_activado_en = None
            existente.recordatorio_enviado = "pendiente"
        else:
            session.add(HandoffEstado(
                telefono=telefono,
                pausado_en=datetime.utcnow(),
                tipo_timer="stop",
                timer_activado_en=None,
                recordatorio_enviado="pendiente",
            ))
        await session.commit()
    logger.info(f"Gian pausado para {telefono} — esperando trigger de CRM")


async def activar_timer(telefono: str, tipo: str):
    """Activa timer desde el CRM. tipo: 'cotizacion' (72hs) o 'visita' (96hs)."""
    async with async_session() as session:
        result = await session.execute(
            select(HandoffEstado).where(HandoffEstado.telefono == telefono)
        )
        existente = result.scalar_one_or_none()

        if existente:
            existente.tipo_timer = tipo
            existente.timer_activado_en = datetime.utcnow()
            existente.recordatorio_enviado = "pendiente"
        else:
            session.add(HandoffEstado(
                telefono=telefono,
                pausado_en=datetime.utcnow(),
                tipo_timer=tipo,
                timer_activado_en=datetime.utcnow(),
                recordatorio_enviado="pendiente",
            ))
        await session.commit()
    logger.info(f"Timer {tipo} activado para {telefono}")


async def reanudar_contacto(telefono: str):
    """Reanuda a Gian — cancela pausa y timers."""
    async with async_session() as session:
        await session.execute(
            delete(HandoffEstado).where(HandoffEstado.telefono == telefono)
        )
        await session.commit()
    logger.info(f"Gian reanudado para {telefono}")


async def esta_pausado(telefono: str) -> bool:
    """Verifica si Gian está pausado para un contacto."""
    async with async_session() as session:
        result = await session.execute(
            select(HandoffEstado).where(HandoffEstado.telefono == telefono)
        )
        return result.scalar_one_or_none() is not None


def _normalizar(texto: str) -> str:
    """Quita puntuación, emojis y espacios repetidos; deja minúsculas."""
    t = texto.strip().lower()
    t = re.sub(r"[^\w\s]", "", t, flags=re.UNICODE)  # quita signos de puntuación/emojis
    t = re.sub(r"\s+", " ", t).strip()
    return t


async def es_comando_stop(texto: str) -> bool:
    """
    Reconoce 'stop gian' aunque venga con mayúsculas, espacios extra o puntuación.
    Ej: "Stop gian", "stop  gian.", "Stop Gian!" -> True
    No matchea si 'stop' o 'gian' aparecen como parte de una frase larga no relacionada,
    para evitar falsos positivos.
    """
    t = _normalizar(texto)
    comandos = {"stop gian", "parar gian", "pausar gian"}
    if t in comandos:
        return True
    # Tolera 1-2 palabras extra alrededor (ej. "porfa stop gian", "stop gian por favor")
    palabras = t.split()
    if len(palabras) <= 5:
        for cmd in comandos:
            if cmd in t:
                return True
    return False


async def es_comando_start(texto: str) -> bool:
    """
    Reconoce 'start gian' aunque venga con mayúsculas, espacios extra o puntuación.
    """
    t = _normalizar(texto)
    comandos = {"start gian", "iniciar gian", "activar gian", "start"}
    if t in comandos:
        return True
    palabras = t.split()
    if len(palabras) <= 5:
        for cmd in comandos:
            if cmd in t:
                return True
    return False


# ── Scheduler de recordatorios ────────────────────────────────────────
async def scheduler_recordatorios(proveedor):
    """
    Revisa cada 5 minutos si hay recordatorios pendientes.
    Solo actúa si el timer fue activado desde el CRM.
    Envía UN SOLO mensaje y deja en pausa definitiva.
    """
    logger.info("Scheduler de recordatorios iniciado")
    while True:
        try:
            await asyncio.sleep(300)  # cada 5 minutos
            ahora = datetime.utcnow()

            async with async_session() as session:
                result = await session.execute(select(HandoffEstado))
                estados = result.scalars().all()

                for estado in estados:
                    if not estado.timer_activado_en:
                        continue
                    if estado.recordatorio_enviado != "pendiente":
                        continue

                    tiempo_desde_timer = ahora - estado.timer_activado_en

                    # Cotización → 72hs → 1 solo mensaje → pausa definitiva
                    if (estado.tipo_timer == "cotizacion" and
                            tiempo_desde_timer >= timedelta(hours=72)):
                        await _guardar_si_no_esta(estado.telefono, MSG_COTIZACION)
                        ok = await proveedor.enviar_mensaje(estado.telefono, MSG_COTIZACION)
                        if ok:
                            estado.recordatorio_enviado = "enviado"
                            estado.timer_activado_en = None
                            await session.commit()
                            logger.info(f"Recordatorio cotización enviado a {estado.telefono} — pausa definitiva")

                    # Visita → 96hs → 1 solo mensaje → pausa definitiva
                    elif (estado.tipo_timer == "visita" and
                            tiempo_desde_timer >= timedelta(hours=96)):
                        await _guardar_si_no_esta(estado.telefono, MSG_VISITA)
                        ok = await proveedor.enviar_mensaje(estado.telefono, MSG_VISITA)
                        if ok:
                            estado.recordatorio_enviado = "enviado"
                            estado.timer_activado_en = None
                            await session.commit()
                            logger.info(f"Recordatorio visita enviado a {estado.telefono} — pausa definitiva")

        except Exception as e:
            logger.error(f"Error en scheduler recordatorios: {e}")

        try:
            await procesar_seguimientos(proveedor)
        except Exception as e:
            logger.error(f"Error en scheduler seguimientos: {e}")


async def procesar_seguimientos(proveedor):
    """
    Seguimientos cuando el cliente deja de responder a mitad del flujo.
    Solo si: Gian no está pausado, el cliente respondió al menos una vez,
    el último mensaje es de Gian y el flujo no terminó (sin LEAD/LLAMADA/LEAD_MADERA).
    24h -> [SEGUIMIENTO:1], 48h -> [SEGUIMIENTO:2], 72h -> [CIERRE_SIN_RESPUESTA]
    (este último NO envía nada al cliente: manda el lead al CRM y pausa).
    """
    from agent.memory import listar_chats_con_actividad, obtener_historial_con_fechas
    from agent.brain import generar_respuesta, obtener_mensaje_error
    from agent.crm import enviar_lead_crm
    from agent.flujo import despachar_respuesta

    ahora = datetime.utcnow()
    for telefono in await listar_chats_con_actividad(ahora - timedelta(days=5)):
        if await esta_pausado(telefono):
            continue

        hist = await obtener_historial_con_fechas(telefono)
        if not hist or hist[-1]["role"] != "assistant":
            continue
        if any(m["role"] == "assistant" and any(t in m["content"] for t in TAGS_FLUJO_TERMINADO)
               for m in hist):
            continue

        # Último mensaje real del cliente (no cuentan los tokens del sistema)
        idx = None
        for i in range(len(hist) - 1, -1, -1):
            m = hist[i]
            if m["role"] == "user" and not m["content"].startswith(TOKENS_SISTEMA):
                idx = i
                break
        if idx is None:
            continue  # el cliente nunca respondió (ej. solo está el mensaje de GHL)

        enviados = sum(
            1 for m in hist[idx + 1:]
            if m["role"] == "user" and m["content"].startswith(TOKENS_SISTEMA)
        )
        if enviados >= len(SEGUIMIENTOS):
            continue
        horas_req, token = SEGUIMIENTOS[enviados]
        if ahora - hist[idx]["timestamp"] < timedelta(hours=horas_req):
            continue

        historial = [{"role": m["role"], "content": m["content"]} for m in hist[-20:]]
        respuesta = await generar_respuesta(token, historial)
        if respuesta == obtener_mensaje_error():
            continue  # falla de la API: se reintenta en la próxima vuelta

        await guardar_mensaje(telefono, "user", token)
        await guardar_mensaje(telefono, "assistant", respuesta)

        if token == "[CIERRE_SIN_RESPUESTA]":
            # Nunca se envía nada al cliente en este paso
            from agent.memory import obtener_historial
            if "[LEAD:" in respuesta:
                await enviar_lead_crm(telefono, "", await obtener_historial(telefono, limite=60))
            await pausar_contacto(telefono)
            logger.info(f"Cierre sin respuesta para {telefono} - lead al CRM y Gian pausado")
        else:
            await despachar_respuesta(proveedor, telefono, "", respuesta, avisar_incompleto=False)
            logger.info(f"{token} enviado a {telefono}")
