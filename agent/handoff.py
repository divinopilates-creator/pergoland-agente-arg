# agent/handoff.py - Handoff humano y recordatorios automáticos
# Timer se activa desde el CRM al cambiar etapa, NO desde stop gian

import asyncio
import logging
from datetime import datetime, timedelta
from sqlalchemy import String, DateTime, Integer, select, delete, text
from sqlalchemy.orm import Mapped, mapped_column
from agent.memory import Base, engine, async_session

logger = logging.getLogger("agentkit")

# ── Mensajes automáticos ──────────────────────────────────────────────
MSG_COTIZACION = (
    "Hola 👋 Soy Gian de Pergoland Argentina. "
    "Quería saber si pudiste revisar la cotización que te enviamos "
    "y si tenés alguna consulta sobre tu proyecto. "
    "¡Estamos para ayudarte! 😊"
)

MSG_VISITA = (
    "Hola 👋 Gian de Pergoland Argentina. "
    "Quería hacer un seguimiento post visita técnica. "
    "¿Pudiste revisar la propuesta con Martín? "
    "¡Cualquier duda estamos acá! 🙌"
)


# ── Modelo de base de datos ───────────────────────────────────────────
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
    """Activa timer desde el CRM. tipo: 'cotizacion' (24hs) o 'visita' (72hs)."""
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


async def es_comando_stop(texto: str) -> bool:
    texto_lower = texto.strip().lower()
    return texto_lower in ["stop gian", "parar gian", "pausar gian"]


async def es_comando_start(texto: str) -> bool:
    texto_lower = texto.strip().lower()
    return any(cmd in texto_lower for cmd in [
        "start gian",
        "iniciar gian",
        "activar gian",
        "start"
    ])


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

                    # Cotización → 24hs → 1 solo mensaje → pausa definitiva
                    if (estado.tipo_timer == "cotizacion" and
                            tiempo_desde_timer >= timedelta(hours=24)):
                        ok = await proveedor.enviar_mensaje(estado.telefono, MSG_COTIZACION)
                        if ok:
                            estado.recordatorio_enviado = "enviado"
                            estado.timer_activado_en = None
                            await session.commit()
                            logger.info(f"Recordatorio cotización enviado a {estado.telefono} — pausa definitiva")

                    # Visita → 72hs → 1 solo mensaje → pausa definitiva
                    elif (estado.tipo_timer == "visita" and
                            tiempo_desde_timer >= timedelta(hours=72)):
                        ok = await proveedor.enviar_mensaje(estado.telefono, MSG_VISITA)
                        if ok:
                            estado.recordatorio_enviado = "enviado"
                            estado.timer_activado_en = None
                            await session.commit()
                            logger.info(f"Recordatorio visita enviado a {estado.telefono} — pausa definitiva")

        except Exception as e:
            logger.error(f"Error en scheduler recordatorios: {e}")
