# agent/handoff.py — Handoff humano y recordatorios automáticos
# Matías se pausa con "stop matias" y reactiva automáticamente a las 24/72hs

import asyncio
import logging
from datetime import datetime, timedelta
from sqlalchemy import String, DateTime, Integer, select, delete
from sqlalchemy.orm import Mapped, mapped_column
from agent.memory import Base, engine, async_session

logger = logging.getLogger("agentkit")

# ── Mensajes automáticos de seguimiento ──────────────────────
MSG_24H = (
    "Hola 👋 Soy Matías de Pergoland Chile. "
    "Quería saber si pudiste revisar la información que te enviamos "
    "y si tienes alguna consulta sobre tu proyecto. "
    "¡Estamos para ayudarte! 😊"
)

MSG_72H = (
    "Hola nuevamente 👋 Matías de Pergoland Chile. "
    "Quería hacer un seguimiento de tu consulta. "
    "Si ya tuviste la visita técnica o necesitas coordinarla, "
    "con gusto te ayudo. ¡Cualquier duda estamos aquí! 🙌"
)


# ── Modelo de base de datos ───────────────────────────────────
class HandoffEstado(Base):
    """Estado de pausa por contacto."""
    __tablename__ = "handoff_estado"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telefono: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    pausado_en: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    recordatorio_24h: Mapped[str] = mapped_column(String(10), default="pendiente")  # pendiente / enviado
    recordatorio_72h: Mapped[str] = mapped_column(String(10), default="pendiente")


async def inicializar_handoff_db():
    """Crea la tabla handoff_estado si no existe."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def pausar_contacto(telefono: str):
    """Pausa a Matías para un contacto específico."""
    async with async_session() as session:
        # Verificar si ya existe
        result = await session.execute(
            select(HandoffEstado).where(HandoffEstado.telefono == telefono)
        )
        existente = result.scalar_one_or_none()

        if existente:
            existente.pausado_en = datetime.utcnow()
            existente.recordatorio_24h = "pendiente"
            existente.recordatorio_72h = "pendiente"
        else:
            session.add(HandoffEstado(
                telefono=telefono,
                pausado_en=datetime.utcnow(),
            ))
        await session.commit()
    logger.info(f"Matías pausado para {telefono}")


async def reanudar_contacto(telefono: str):
    """Reanuda a Matías para un contacto (al responder el cliente)."""
    async with async_session() as session:
        await session.execute(
            delete(HandoffEstado).where(HandoffEstado.telefono == telefono)
        )
        await session.commit()
    logger.info(f"Matías reanudado para {telefono}")


async def esta_pausado(telefono: str) -> bool:
    """Verifica si Matías está pausado para un contacto."""
    async with async_session() as session:
        result = await session.execute(
            select(HandoffEstado).where(HandoffEstado.telefono == telefono)
        )
        return result.scalar_one_or_none() is not None


async def es_comando_stop(texto: str) -> bool:
    """Detecta si el mensaje es el comando de pausa."""
    texto_lower = texto.strip().lower()
    return texto_lower in ["stop matias", "stop matías", "parar matias", "parar matías"]


# ── Scheduler de recordatorios ────────────────────────────────
async def scheduler_recordatorios(proveedor):
    """
    Corre en background. Cada 5 minutos revisa si hay recordatorios pendientes.
    Envía el de 24hs y el de 72hs cuando corresponde.
    """
    logger.info("Scheduler de recordatorios iniciado")
    while True:
        try:
            await asyncio.sleep(300)  # revisar cada 5 minutos
            ahora = datetime.utcnow()

            async with async_session() as session:
                result = await session.execute(select(HandoffEstado))
                estados = result.scalars().all()

                for estado in estados:
                    tiempo_pausado = ahora - estado.pausado_en

                    # Recordatorio 24hs
                    if (tiempo_pausado >= timedelta(hours=24) and
                            estado.recordatorio_24h == "pendiente"):
                        ok = await proveedor.enviar_mensaje(estado.telefono, MSG_24H)
                        if ok:
                            estado.recordatorio_24h = "enviado"
                            await session.commit()
                            logger.info(f"Recordatorio 24hs enviado a {estado.telefono}")

                    # Recordatorio 72hs
                    if (tiempo_pausado >= timedelta(hours=72) and
                            estado.recordatorio_72h == "pendiente"):
                        ok = await proveedor.enviar_mensaje(estado.telefono, MSG_72H)
                        if ok:
                            estado.recordatorio_72h = "enviado"
                            await session.commit()
                            logger.info(f"Recordatorio 72hs enviado a {estado.telefono}")

        except Exception as e:
            logger.error(f"Error en scheduler recordatorios: {e}")
