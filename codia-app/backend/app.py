"""
CodIA - Backend API (FastAPI)
Proyecto de grado - Unidad Educativa Fiscomisional Don Bosco

Versión 2: agrega
  1) Base de datos (SQLite + SQLAlchemy) para que las conversaciones
     persistan de verdad, en vez de vivir solo en el localStorage del
     navegador.
  2) Subida de imágenes: el usuario puede adjuntar una foto (por ejemplo,
     una captura de un error o de código) y Gemini la analiza (Gemini es
     multimodal). El respaldo Groq/Llama sigue siendo solo texto.
  3) Ejecución real de código: cuando la IA sugiere un bloque de código,
     el frontend muestra un botón "Ejecutar" que corre ese código de
     verdad usando Piston (https://github.com/engineer-man/piston), una
     API pública y gratuita de sandboxing, y devuelve la salida real.

Para ejecutarlo:
    1. cd backend
    2. python -m venv venv && source venv/bin/activate   (Windows: venv\\Scripts\\activate)
    3. pip install -r requirements.txt
    4. copiar .env.example a .env y llenar tus claves
    5. uvicorn app:app --reload --host 0.0.0.0 --port 8000

La API queda en http://localhost:8000  (documentación automática en /docs)
La base de datos se crea sola la primera vez, en backend/codia.db
"""

import os
import io
import uuid
import base64
from datetime import datetime
from typing import List, Optional

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, ForeignKey
from sqlalchemy.orm import declarative_base, relationship, sessionmaker, Session

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
# Nombre del modelo de Gemini que genera imágenes (Google puede cambiar el
# nombre con el tiempo; si deja de funcionar, revisa el modelo vigente en
# https://ai.google.dev/gemini-api/docs/image-generation y actualiza aquí
# o en la variable de entorno GEMINI_IMAGE_MODEL).
GEMINI_IMAGE_MODEL = os.getenv("GEMINI_IMAGE_MODEL", "gemini-2.5-flash-image")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOADS_DIR = os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOADS_DIR, exist_ok=True)

# =========================================================
# 1) BASE DE DATOS (SQLite por simplicidad; el mismo código de
#    SQLAlchemy funciona con Postgres/MySQL solo cambiando DATABASE_URL,
#    algo recomendable si despliegas en un host con disco no persistente).
# =========================================================
DATABASE_URL = f"sqlite:///{os.path.join(BASE_DIR, 'codia.db')}"
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


class Usuario(Base):
    """Un 'usuario' aquí es solo un identificador anónimo generado en el
    dispositivo (no hay login todavía, ver README -> Qué extendería primero)."""
    __tablename__ = "usuarios"
    id = Column(String, primary_key=True)
    creado_en = Column(DateTime, default=datetime.utcnow)
    conversaciones = relationship("Conversacion", back_populates="usuario")


class Conversacion(Base):
    __tablename__ = "conversaciones"
    id = Column(Integer, primary_key=True, autoincrement=True)
    usuario_id = Column(String, ForeignKey("usuarios.id"), nullable=False)
    titulo = Column(String, default="Nuevo chat")
    creado_en = Column(DateTime, default=datetime.utcnow)
    usuario = relationship("Usuario", back_populates="conversaciones")
    mensajes = relationship(
        "Mensaje", back_populates="conversacion", cascade="all, delete-orphan"
    )


class Mensaje(Base):
    __tablename__ = "mensajes"
    id = Column(Integer, primary_key=True, autoincrement=True)
    conversacion_id = Column(Integer, ForeignKey("conversaciones.id"), nullable=False)
    rol = Column(String, nullable=False)  # "user" o "assistant"
    contenido = Column(Text, nullable=False)
    imagen_url = Column(String, nullable=True)
    documento_url = Column(String, nullable=True)
    documento_nombre = Column(String, nullable=True)
    creado_en = Column(DateTime, default=datetime.utcnow)
    conversacion = relationship("Conversacion", back_populates="mensajes")


Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# =========================================================
# Identidad de la IA
# =========================================================
INSTRUCCION_SISTEMA = (
    "Eres CodIA, un asistente de programación amigable creado para un proyecto de grado "
    "de bachillerato técnico de la Unidad Educativa Fiscomisional Don Bosco (3ro Técnico B, "
    "Periodo 2026-2027). Tus creadores y desarrolladores son los estudiantes Aland Lastra, "
    "Andrés Santander y Anthony Vargas. Si el usuario te pregunta quién te creó, quiénes son "
    "tus creadores, quién te hizo o sobre el proyecto, debes responder detallando con orgullo "
    "estos nombres y los datos de tu institución educativa. Si el usuario te envía una imagen, "
    "descríbela y, si contiene código o un error, explica qué hace o qué falla. Si el mensaje "
    "incluye texto extraído de un documento adjunto (PDF o Word), úsalo como contexto principal "
    "de tu respuesta y coméntale al usuario un resumen breve de qué trata antes de responder su "
    "pregunta puntual. Cuando sugieras "
    "código, usa siempre bloques delimitados con triple backtick y el nombre del lenguaje "
    "(por ejemplo ```python), porque el usuario puede ejecutarlo desde la app. Cuando expliques "
    "un algoritmo, un flujo de control o una arquitectura y un diagrama ayude a entenderlo mejor, "
    "genera también un bloque ```mermaid con un flowchart válido en sintaxis Mermaid (no lo "
    "expliques en palabras si ya lo dibujaste). Responde siempre "
    "en español, de forma clara, breve (máximo 3-4 frases fuera de los bloques de código) y con "
    "buena actitud."
)

_cliente_gemini = None
_cliente_groq = None
GEMINI_DISPONIBLE = False
GROQ_DISPONIBLE = False

if GEMINI_API_KEY:
    try:
        from google import genai

        _cliente_gemini = genai.Client(api_key=GEMINI_API_KEY)
        GEMINI_DISPONIBLE = True
    except Exception:
        GEMINI_DISPONIBLE = False

if GROQ_API_KEY:
    try:
        from groq import Groq

        _cliente_groq = Groq(api_key=GROQ_API_KEY)
        GROQ_DISPONIBLE = True
    except Exception:
        GROQ_DISPONIBLE = False


def extraer_texto_documento(datos_bytes: bytes, mime: str, nombre: str) -> str:
    """Extrae texto de un PDF o un Word (.docx). Se trunca para no gastar
    de más en tokens de la IA con documentos larguísimos."""
    MAX_CARACTERES = 6000
    texto = ""
    try:
        if "pdf" in (mime or "") or nombre.lower().endswith(".pdf"):
            from pypdf import PdfReader

            lector = PdfReader(io.BytesIO(datos_bytes))
            texto = "\n".join((pagina.extract_text() or "") for pagina in lector.pages)
        elif nombre.lower().endswith(".docx") or "wordprocessingml" in (mime or ""):
            from docx import Document

            documento = Document(io.BytesIO(datos_bytes))
            texto = "\n".join(parrafo.text for parrafo in documento.paragraphs)
        else:
            return "[Formato de documento no soportado; solo se aceptan PDF y Word (.docx)]"
    except Exception as e:
        return f"[No se pudo leer el documento: {e}]"

    texto = texto.strip()
    if len(texto) > MAX_CARACTERES:
        texto = texto[:MAX_CARACTERES] + "\n[...documento truncado por longitud...]"
    return texto or "[El documento no tiene texto extraíble (¿es una imagen escaneada?)]"


def generar_imagen_ia(prompt: str) -> bytes:
    """Genera una imagen con Gemini a partir de una descripción en texto.
    Lanza HTTPException si no hay proveedor de imágenes disponible."""
    if not GEMINI_DISPONIBLE:
        raise HTTPException(
            status_code=503,
            detail="La generación de imágenes necesita Gemini configurado (Groq no genera imágenes).",
        )
    try:
        respuesta = _cliente_gemini.models.generate_content(
            model=GEMINI_IMAGE_MODEL,
            contents=[prompt],
        )
        for parte in respuesta.candidates[0].content.parts:
            datos_inline = getattr(parte, "inline_data", None)
            if datos_inline and getattr(datos_inline, "data", None):
                return datos_inline.data
        raise ValueError("La respuesta de Gemini no incluyó ninguna imagen.")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"No se pudo generar la imagen: {e}")



def generar_respuesta_ia(
    mensaje_usuario: str,
    historial: List[Mensaje],
    imagen_base64: Optional[str] = None,
    imagen_mime: Optional[str] = None,
):
    """Devuelve (texto_respuesta, proveedor_usado)."""

    if GEMINI_DISPONIBLE:
        try:
            from google.genai import types

            contenidos = []
            for m in historial:
                rol_gemini = "user" if m.rol == "user" else "model"
                contenidos.append({"role": rol_gemini, "parts": [{"text": m.contenido}]})

            partes_usuario = [{"text": mensaje_usuario or "Analiza esta imagen."}]
            if imagen_base64:
                partes_usuario.append(
                    {"inline_data": {"mime_type": imagen_mime or "image/png", "data": imagen_base64}}
                )
            contenidos.append({"role": "user", "parts": partes_usuario})

            respuesta = _cliente_gemini.models.generate_content(
                model="gemini-flash-latest",
                contents=contenidos,
                config=types.GenerateContentConfig(system_instruction=INSTRUCCION_SISTEMA),
            )
            return respuesta.text, "gemini"
        except Exception:
            pass  # seguimos al plan B

    if GROQ_DISPONIBLE:
        try:
            mensajes_groq = [{"role": "system", "content": INSTRUCCION_SISTEMA}]
            for m in historial:
                rol_groq = "user" if m.rol == "user" else "assistant"
                mensajes_groq.append({"role": rol_groq, "content": m.contenido})

            texto_usuario = mensaje_usuario
            if imagen_base64:
                texto_usuario += (
                    "\n\n[El usuario adjuntó una imagen, pero el sistema de respaldo actual "
                    "no puede analizar imágenes; solo Gemini puede. Avísale de esto amablemente.]"
                )
            mensajes_groq.append({"role": "user", "content": texto_usuario})

            respuesta_groq = _cliente_groq.chat.completions.create(
                model="llama-3.1-8b-instant",
                messages=mensajes_groq,
                temperature=0.7,
            )
            return respuesta_groq.choices[0].message.content, "groq"
        except Exception as error_groq:
            raise HTTPException(
                status_code=502,
                detail=f"Ambos proveedores de IA fallaron. Último error (Groq): {error_groq}",
            )

    raise HTTPException(
        status_code=503,
        detail="No hay ninguna API de IA configurada. Revisa tu archivo .env",
    )


# =========================================================
# 3) EJECUCIÓN DE CÓDIGO (Piston - https://github.com/engineer-man/piston)
# =========================================================
PISTON_URL = "https://emkc.org/api/v2/piston"
_runtimes_cache = None

ALIAS_LENGUAJE = {
    "python": "python", "python3": "python", "py": "python",
    "javascript": "javascript", "js": "javascript", "node": "javascript",
    "typescript": "typescript", "ts": "typescript",
    "java": "java",
    "c": "c",
    "cpp": "cpp", "c++": "cpp",
    "csharp": "csharp", "c#": "csharp",
    "php": "php",
    "ruby": "ruby", "rb": "ruby",
    "go": "go", "golang": "go",
}


def obtener_runtimes():
    global _runtimes_cache
    if _runtimes_cache is None:
        respuesta = requests.get(f"{PISTON_URL}/runtimes", timeout=10)
        respuesta.raise_for_status()
        _runtimes_cache = respuesta.json()
    return _runtimes_cache


# =========================================================
# Modelos de datos de la API (Pydantic)
# =========================================================
class MensajeOut(BaseModel):
    id: int
    rol: str
    contenido: str
    imagen_url: Optional[str] = None
    documento_url: Optional[str] = None
    documento_nombre: Optional[str] = None
    creado_en: datetime

    class Config:
        from_attributes = True


class ConversacionOut(BaseModel):
    id: int
    titulo: str
    creado_en: datetime

    class Config:
        from_attributes = True


class CrearConversacion(BaseModel):
    usuario_id: str


class SolicitudChat(BaseModel):
    usuario_id: str
    conversacion_id: int
    mensaje: str
    imagen_base64: Optional[str] = None  # base64 puro, sin el prefijo data:...
    imagen_mime: Optional[str] = None
    documento_base64: Optional[str] = None
    documento_mime: Optional[str] = None
    documento_nombre: Optional[str] = None


class RespuestaChat(BaseModel):
    respuesta: str
    proveedor: str
    conversacion: ConversacionOut


class SolicitudEjecutar(BaseModel):
    lenguaje: str
    codigo: str


class RespuestaEjecutar(BaseModel):
    salida: str
    error: str
    lenguaje_usado: str
    version_usada: str


class SolicitudGenerarImagen(BaseModel):
    usuario_id: str
    conversacion_id: int
    prompt: str


class RespuestaGenerarImagen(BaseModel):
    imagen_url: str
    conversacion: ConversacionOut


# =========================================================
# Aplicación FastAPI
# =========================================================
app = FastAPI(title="CodIA API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Sirve las imágenes subidas en /api/imagenes/<archivo>
app.mount("/api/imagenes", StaticFiles(directory=UPLOADS_DIR), name="imagenes")


def obtener_o_crear_usuario(db: Session, usuario_id: str) -> Usuario:
    usuario = db.get(Usuario, usuario_id)
    if usuario is None:
        usuario = Usuario(id=usuario_id)
        db.add(usuario)
        db.commit()
        db.refresh(usuario)
    return usuario


@app.get("/api/estado")
def estado():
    return {
        "gemini_disponible": GEMINI_DISPONIBLE,
        "groq_disponible": GROQ_DISPONIBLE,
        "imagenes_disponible": GEMINI_DISPONIBLE,  # solo Gemini analiza imágenes
        "ejecutar_codigo_disponible": True,
        "generar_imagenes_disponible": GEMINI_DISPONIBLE,
        "documentos_disponible": GEMINI_DISPONIBLE or GROQ_DISPONIBLE,
    }


@app.post("/api/conversaciones", response_model=ConversacionOut)
def crear_conversacion(datos: CrearConversacion, db: Session = Depends(get_db)):
    obtener_o_crear_usuario(db, datos.usuario_id)
    conversacion = Conversacion(usuario_id=datos.usuario_id, titulo="Nuevo chat")
    db.add(conversacion)
    db.commit()
    db.refresh(conversacion)
    return conversacion


@app.get("/api/conversaciones", response_model=List[ConversacionOut])
def listar_conversaciones(usuario_id: str, db: Session = Depends(get_db)):
    return (
        db.query(Conversacion)
        .filter(Conversacion.usuario_id == usuario_id)
        .order_by(Conversacion.creado_en.desc())
        .all()
    )


@app.get("/api/conversaciones/{conversacion_id}/mensajes", response_model=List[MensajeOut])
def listar_mensajes(conversacion_id: int, db: Session = Depends(get_db)):
    conversacion = db.get(Conversacion, conversacion_id)
    if conversacion is None:
        raise HTTPException(status_code=404, detail="Conversación no encontrada")
    return (
        db.query(Mensaje)
        .filter(Mensaje.conversacion_id == conversacion_id)
        .order_by(Mensaje.creado_en.asc())
        .all()
    )


@app.delete("/api/conversaciones/{conversacion_id}")
def borrar_conversacion(conversacion_id: int, db: Session = Depends(get_db)):
    conversacion = db.get(Conversacion, conversacion_id)
    if conversacion is None:
        raise HTTPException(status_code=404, detail="Conversación no encontrada")
    db.delete(conversacion)
    db.commit()
    return {"ok": True}


@app.post("/api/chat", response_model=RespuestaChat)
def chat(solicitud: SolicitudChat, db: Session = Depends(get_db)):
    if not solicitud.mensaje.strip() and not solicitud.imagen_base64:
        raise HTTPException(status_code=400, detail="El mensaje no puede estar vacío")

    conversacion = db.get(Conversacion, solicitud.conversacion_id)
    if conversacion is None:
        raise HTTPException(status_code=404, detail="Conversación no encontrada")

    # Guardar la imagen adjunta (si la hay) como archivo en disco
    imagen_url = None
    if solicitud.imagen_base64:
        try:
            extension = "png" if "png" in (solicitud.imagen_mime or "") else "jpg"
            nombre_archivo = f"{uuid.uuid4().hex}.{extension}"
            ruta_archivo = os.path.join(UPLOADS_DIR, nombre_archivo)
            with open(ruta_archivo, "wb") as f:
                f.write(base64.b64decode(solicitud.imagen_base64))
            imagen_url = f"/api/imagenes/{nombre_archivo}"
        except Exception:
            raise HTTPException(status_code=400, detail="La imagen enviada no es válida")

    # Guardar el documento adjunto (si lo hay) y extraer su texto como contexto
    documento_url = None
    mensaje_para_ia = solicitud.mensaje
    if solicitud.documento_base64:
        try:
            datos_documento = base64.b64decode(solicitud.documento_base64)
            nombre_original = solicitud.documento_nombre or "documento"
            extension = "pdf" if nombre_original.lower().endswith(".pdf") else "docx"
            nombre_archivo = f"{uuid.uuid4().hex}.{extension}"
            ruta_archivo = os.path.join(UPLOADS_DIR, nombre_archivo)
            with open(ruta_archivo, "wb") as f:
                f.write(datos_documento)
            documento_url = f"/api/imagenes/{nombre_archivo}"

            texto_extraido = extraer_texto_documento(
                datos_documento, solicitud.documento_mime or "", nombre_original
            )
            mensaje_para_ia = (
                f"{solicitud.mensaje}\n\n[Documento adjunto: {nombre_original}]\n{texto_extraido}"
            )
        except Exception:
            raise HTTPException(status_code=400, detail="El documento enviado no es válido")

    # Historial previo (para dar contexto a la IA)
    historial_previo = (
        db.query(Mensaje)
        .filter(Mensaje.conversacion_id == conversacion.id)
        .order_by(Mensaje.creado_en.asc())
        .all()
    )

    # Guardar el mensaje del usuario (el texto que escribió, no el texto extendido para la IA)
    mensaje_usuario = Mensaje(
        conversacion_id=conversacion.id,
        rol="user",
        contenido=solicitud.mensaje,
        imagen_url=imagen_url,
        documento_url=documento_url,
        documento_nombre=solicitud.documento_nombre if documento_url else None,
    )
    db.add(mensaje_usuario)

    # Si es el primer mensaje, lo usamos para titular la conversación
    if conversacion.titulo == "Nuevo chat" and solicitud.mensaje.strip():
        titulo = solicitud.mensaje.strip()[:30]
        conversacion.titulo = titulo + ("..." if len(solicitud.mensaje.strip()) > 30 else "")

    db.commit()

    # Generar la respuesta de la IA (con el texto extendido si había documento)
    texto, proveedor = generar_respuesta_ia(
        mensaje_para_ia,
        historial_previo,
        imagen_base64=solicitud.imagen_base64,
        imagen_mime=solicitud.imagen_mime,
    )

    mensaje_ia = Mensaje(conversacion_id=conversacion.id, rol="assistant", contenido=texto)
    db.add(mensaje_ia)
    db.commit()
    db.refresh(conversacion)

    return RespuestaChat(respuesta=texto, proveedor=proveedor, conversacion=conversacion)


@app.post("/api/ejecutar-codigo", response_model=RespuestaEjecutar)
def ejecutar_codigo(solicitud: SolicitudEjecutar):
    lenguaje_normalizado = ALIAS_LENGUAJE.get(solicitud.lenguaje.strip().lower())
    if lenguaje_normalizado is None:
        raise HTTPException(
            status_code=400,
            detail=f"Lenguaje '{solicitud.lenguaje}' no soportado para ejecución.",
        )

    try:
        runtimes = obtener_runtimes()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"No se pudo contactar a Piston: {e}")

    coincidencia = next((r for r in runtimes if r["language"] == lenguaje_normalizado), None)
    if coincidencia is None:
        raise HTTPException(
            status_code=400, detail=f"Piston no tiene runtime para '{lenguaje_normalizado}'."
        )

    extensiones = {
        "python": "py", "javascript": "js", "typescript": "ts", "java": "java",
        "c": "c", "cpp": "cpp", "csharp": "cs", "php": "php", "ruby": "rb", "go": "go",
    }
    nombre_archivo = f"main.{extensiones.get(lenguaje_normalizado, 'txt')}"

    try:
        respuesta = requests.post(
            f"{PISTON_URL}/execute",
            json={
                "language": lenguaje_normalizado,
                "version": coincidencia["version"],
                "files": [{"name": nombre_archivo, "content": solicitud.codigo}],
            },
            timeout=15,
        )
        respuesta.raise_for_status()
        datos = respuesta.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Error ejecutando el código: {e}")

    run = datos.get("run", {})
    return RespuestaEjecutar(
        salida=run.get("stdout", ""),
        error=run.get("stderr", "") or datos.get("compile", {}).get("stderr", ""),
        lenguaje_usado=lenguaje_normalizado,
        version_usada=coincidencia["version"],
    )


@app.post("/api/generar-imagen", response_model=RespuestaGenerarImagen)
def generar_imagen(solicitud: SolicitudGenerarImagen, db: Session = Depends(get_db)):
    if not solicitud.prompt.strip():
        raise HTTPException(status_code=400, detail="Describe qué imagen quieres generar")

    conversacion = db.get(Conversacion, solicitud.conversacion_id)
    if conversacion is None:
        raise HTTPException(status_code=404, detail="Conversación no encontrada")

    # Guardar el pedido del usuario en el historial
    mensaje_usuario = Mensaje(
        conversacion_id=conversacion.id,
        rol="user",
        contenido=f"🎨 Genera una imagen: {solicitud.prompt}",
    )
    db.add(mensaje_usuario)
    if conversacion.titulo == "Nuevo chat":
        conversacion.titulo = "🎨 " + solicitud.prompt.strip()[:26]
    db.commit()

    # Generar la imagen y guardarla en disco
    imagen_bytes = generar_imagen_ia(solicitud.prompt)
    nombre_archivo = f"{uuid.uuid4().hex}.png"
    with open(os.path.join(UPLOADS_DIR, nombre_archivo), "wb") as f:
        f.write(imagen_bytes)
    imagen_url = f"/api/imagenes/{nombre_archivo}"

    mensaje_ia = Mensaje(
        conversacion_id=conversacion.id,
        rol="assistant",
        contenido=f"Aquí tienes la imagen que pediste: “{solicitud.prompt}”",
        imagen_url=imagen_url,
    )
    db.add(mensaje_ia)
    db.commit()
    db.refresh(conversacion)

    return RespuestaGenerarImagen(imagen_url=imagen_url, conversacion=conversacion)

@app.get("/")
def read_root():
    return {"message": "API de CodIA funcionando correctamente"}
