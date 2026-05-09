from datetime import datetime
from sqlalchemy import Column, String, Boolean, Integer, DateTime, Text, Float, JSON, ForeignKey
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


class Campana(Base):
    __tablename__ = "campanas"
    id = Column(Integer, primary_key=True, autoincrement=True)
    nombre = Column(String(200), nullable=False)
    archivo_origen = Column(String(500))
    mensaje_template = Column(Text)
    total_filas = Column(Integer, default=0)
    total_numeros = Column(Integer, default=0)
    estado = Column(String(50), default="pendiente")
    creada_en = Column(DateTime, default=datetime.utcnow)
    completada_en = Column(DateTime, nullable=True)
    envios = relationship("Envio", back_populates="campana")


class Cliente(Base):
    __tablename__ = "clientes"
    id = Column(Integer, primary_key=True, autoincrement=True)
    numero_original = Column(String(100))
    numeros_e164 = Column(JSON)           # Lista de números extraídos de la celda
    nombre = Column(String(200), nullable=True)
    datos_extra = Column(JSON, default=dict)
    campana_id = Column(Integer, ForeignKey("campanas.id"))
    creado_en = Column(DateTime, default=datetime.utcnow)
    envios = relationship("Envio", back_populates="cliente")
    respuestas = relationship("Respuesta", back_populates="cliente")


class Envio(Base):
    __tablename__ = "envios"
    id = Column(Integer, primary_key=True, autoincrement=True)
    campana_id = Column(Integer, ForeignKey("campanas.id"))
    cliente_id = Column(Integer, ForeignKey("clientes.id"))
    numero_destino = Column(String(20))
    mensaje_enviado = Column(Text)
    estado = Column(String(50), default="pendiente")
    meta_message_id = Column(String(200), nullable=True)
    error_detalle = Column(Text, nullable=True)
    enviado_en = Column(DateTime, nullable=True)
    creado_en = Column(DateTime, default=datetime.utcnow)
    campana = relationship("Campana", back_populates="envios")
    cliente = relationship("Cliente", back_populates="envios")


class Respuesta(Base):
    __tablename__ = "respuestas"
    id = Column(Integer, primary_key=True, autoincrement=True)
    cliente_id = Column(Integer, ForeignKey("clientes.id"), nullable=True)
    numero_origen = Column(String(20), index=True)
    texto = Column(Text)
    clasificacion = Column(String(50), nullable=True)
    score_interes = Column(Float, nullable=True)
    alerta_emitida = Column(Boolean, default=False)
    meta_message_id = Column(String(200), nullable=True)
    recibida_en = Column(DateTime, default=datetime.utcnow)
    cliente = relationship("Cliente", back_populates="respuestas")


class AlertaInteresado(Base):
    __tablename__ = "alertas"
    id = Column(Integer, primary_key=True, autoincrement=True)
    cliente_id = Column(Integer, ForeignKey("clientes.id"), nullable=True)
    numero = Column(String(20))
    nombre = Column(String(200), nullable=True)
    ultimo_mensaje = Column(Text)
    score = Column(Float)
    clasificacion = Column(String(50))
    vista = Column(Boolean, default=False)
    creada_en = Column(DateTime, default=datetime.utcnow)
