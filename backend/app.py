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
from fastapi.responses import StreamingResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, ForeignKey, LargeBinary
from sqlalchemy.orm import declarative_base, relationship, sessionmaker, Session

load_dotenv()

# Las claves se leen en claves.py: soporta varias por proveedor
# (GEMINI_API_KEYS=a,b,c  o  GEMINI_API_KEY_2, _3...) para repartir la carga
# y no saturar el límite gratuito de una sola clave.
from claves import PoolClaves, leer_claves
# Nombre del modelo de Gemini que genera imágenes (Google puede cambiar el
# nombre con el tiempo; si deja de funcionar, revisa el modelo vigente en
# https://ai.google.dev/gemini-api/docs/image-generation y actualiza aquí
# o en la variable de entorno GEMINI_IMAGE_MODEL).
GEMINI_IMAGE_MODEL = os.getenv("GEMINI_IMAGE_MODEL", "gemini-2.5-flash-image")
# Nombre del modelo de Whisper (transcripción de voz) que ofrece Groq.
GROQ_WHISPER_MODEL = os.getenv("GROQ_WHISPER_MODEL", "whisper-large-v3-turbo")
# Nombre del modelo de chat de Groq. Groq retira/renombra modelos con cierta
# frecuencia (retiraron llama-3.1-8b-instant y llama-3.3-70b-versatile en
# junio de 2026); si este deja de funcionar, revisa la lista vigente en
# https://console.groq.com/docs/models o https://console.groq.com/docs/deprecations
GROQ_CHAT_MODEL = os.getenv("GROQ_CHAT_MODEL", "openai/gpt-oss-20b")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOADS_DIR = os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOADS_DIR, exist_ok=True)

# =========================================================
# 1) BASE DE DATOS
#    Usa Postgres si la variable de entorno DATABASE_URL está puesta
#    (así queda persistente de verdad en Render, a diferencia de SQLite en
#    disco, que se borra en cada redeploy del plan gratuito). Si no está
#    puesta (por ejemplo, corriendo en tu compu), cae a un archivo SQLite
#    local — el mismo código de SQLAlchemy funciona igual con ambas.
# =========================================================
def _limpiar_url_bd(valor: str) -> str:
    """Tolera errores típicos al pegar la cadena de Neon: comillas, espacios,
    el prefijo "psql " o "DATABASE_URL="."""
    v = (valor or "").strip()
    if v.lower().startswith("psql"):
        v = v[4:].strip()
    if v.upper().startswith("DATABASE_URL="):
        v = v.split("=", 1)[1].strip()
    return v.strip("'\"").strip()


DATABASE_URL = _limpiar_url_bd(os.getenv("DATABASE_URL")) or f"sqlite:///{os.path.join(BASE_DIR, 'codia.db')}"

# Render entrega la URL de Postgres con el prefijo "postgres://", pero
# SQLAlchemy 2.x exige "postgresql://" — lo corregimos automáticamente.
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# SQLAlchemy 2.1+ usa por defecto el driver "psycopg" (v3) para postgresql://,
# pero nosotros instalamos psycopg2. Lo indicamos explícitamente para que
# funcione con cualquier versión de SQLAlchemy.
if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg2://", 1)

_argumentos_conexion = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
# pool_pre_ping: Neon "duerme" la base tras unos minutos sin uso y cierra las
# conexiones; esto comprueba la conexión antes de usarla y reconecta sola.
engine = create_engine(DATABASE_URL, connect_args=_argumentos_conexion, pool_pre_ping=True, pool_recycle=300)
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


class Archivo(Base):
    """Imágenes y documentos guardados DENTRO de la base de datos. El disco de
    Render se borra en cada redeploy; la base de datos (Neon) no, así las
    imágenes de los chats siguen viéndose siempre."""
    __tablename__ = "archivos"
    nombre = Column(String, primary_key=True)
    mime = Column(String, nullable=False)
    datos = Column(LargeBinary, nullable=False)
    creado_en = Column(DateTime, default=datetime.utcnow)


Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


MIME_POR_EXTENSION = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "webp": "image/webp",
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


def extension_de_imagen(datos: bytes) -> str:
    """Detecta el formato real por los primeros bytes (los proveedores de
    imágenes a veces devuelven JPEG o WebP en vez de PNG)."""
    if datos[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if datos[:3] == b"\xff\xd8\xff":
        return "jpg"
    if datos[:4] == b"RIFF" and datos[8:12] == b"WEBP":
        return "webp"
    return "png"


def guardar_archivo(nombre: str, datos: bytes) -> None:
    """Guarda el archivo en la base de datos (permanente) y en disco (copia
    rápida). Si la base falla, igual queda en disco y se avisa en los logs."""
    with open(os.path.join(UPLOADS_DIR, nombre), "wb") as f:
        f.write(datos)
    mime = MIME_POR_EXTENSION.get(nombre.rsplit(".", 1)[-1], "application/octet-stream")
    db = SessionLocal()
    try:
        db.merge(Archivo(nombre=nombre, mime=mime, datos=datos))
        db.commit()
    except Exception as e:  # noqa: BLE001
        db.rollback()
        print(f"[CodIA] No se pudo guardar {nombre} en la base de datos: {e}", flush=True)
    finally:
        db.close()


# =========================================================
# Identidad de la IA
# =========================================================
INSTRUCCION_SISTEMA = (
    "Eres CodIA, un asistente inteligente y amigable creado para un proyecto de grado de bachillerato "
    "técnico de la Unidad Educativa Fiscomisional Don Bosco (3ro Técnico B, Periodo 2026-2027). Tus "
    "creadores y desarrolladores son los estudiantes Aland Lastra, Andrés Santander y Anthony Vargas. "
    "Si el usuario te pregunta quién te creó, quiénes son tus creadores, quién te hizo o sobre el "
    "proyecto, responde con orgullo detallando estos nombres y los datos de tu institución educativa.\n\n"
    "ALCANCE: puedes ayudar con CUALQUIER tema: ciencias, matemáticas, historia, lengua, idiomas, "
    "geografía, arte, salud en general, deportes, tareas escolares, cultura general, consejos de estudio, "
    "redacción, ideas y conversación casual. Tu especialidad y mayor profundidad es la programación y la "
    "tecnología, donde das respuestas expertas; pero nunca rechaces una pregunta solo porque no sea de "
    "programación: respóndela bien. En temas delicados (salud, leyes, dinero) da información útil y "
    "responsable y recomienda acudir a un profesional cuando corresponda.\n\n"
    "CÓMO RAZONAR Y RESPONDER:\n"
    "- Primero entiende qué necesita realmente la persona y responde a eso. Piensa el problema antes de "
    "responder: para preguntas de lógica, matemáticas, comparaciones o decisiones, razona paso a paso "
    "con palabras y da la conclusión clara.\n"
    "- NO todo se resuelve con código. Si la pregunta es conceptual, de opinión, de conocimiento "
    "general, de estudio, de lógica o una conversación normal, responde con explicación en texto, "
    "ejemplos de la vida real y analogías, SIN bloques de código.\n"
    "- Escribe código solo cuando la persona lo pida o cuando un ejemplo corto realmente ayude. "
    "Cuando lo hagas, usa bloques con triple backtick y el nombre del lenguaje (por ejemplo ```python), "
    "porque el usuario puede ejecutarlo desde la app, y explica brevemente qué hace.\n"
    "- Adapta la longitud: respuestas cortas para preguntas simples y más detalladas cuando el tema lo "
    "requiera. No rellenes. Usa tablas cuando compares cosas y listas cuando enumeres pasos.\n"
    "- Si falta información importante, pregunta lo mínimo necesario. Si no sabes algo, dilo con "
    "honestidad en lugar de inventar.\n\n"
    "ORGANIZADORES GRÁFICOS Y FÍSICA: cuando te pidan un organizador gráfico (mapa mental, cuadro "
    "sinóptico, esquema, mapa conceptual, organigrama, línea de tiempo, pasos, ciclo, diagrama de Venn, "
    "causa-efecto o espina de pescado) o un diagrama de cuerpo libre de física, entrégalo en UN bloque "
    "```organizador que contenga SOLO un JSON válido (comillas dobles, sin comentarios ni comas "
    "sobrantes). La aplicación lo dibuja con un diseño profesional, así que NO lo describas con texto "
    "ni uses mermaid para estos casos. Antes o después del bloque explica brevemente el contenido (y en "
    "física, el razonamiento y los cálculos). Formatos disponibles (\"t\" es siempre el texto corto):\n"
    "  * Mapa mental (radial): {\"tipo\":\"mapa-mental\",\"titulo\":\"Tema\",\"nodos\":[{\"t\":\"Rama\","
    "\"e\":\"🪐\",\"hijos\":[{\"t\":\"Idea\"},{\"t\":\"Idea\",\"hijos\":[{\"t\":\"Detalle\"}]}]}]}\n"
    "  * Cuadro sinóptico (llaves de izquierda a derecha): igual pero con \"tipo\":\"esquema\".\n"
    "  * Organigrama / jerarquía de arriba hacia abajo: igual pero con \"tipo\":\"organigrama\".\n"
    "  * MAPA CONCEPTUAL (el formato por defecto cuando pidan un mapa conceptual; se dibuja con "
    "tarjetas de colores, frases de enlace y viñetas): {\"tipo\":\"mapa-conceptual\",\"titulo\":\"Los "
    "Ecosistemas\",\"definicion\":\"Son: conjuntos de seres vivos y el medio físico en el que "
    "interactúan.\",\"ramas\":[{\"t\":\"Componentes\",\"enlace\":\"Se dividen en:\",\"hijos\":[{\"t\":\"Bióticos\","
    "\"d\":\"(seres vivos)\",\"items\":[\"Plantas\",\"Animales\"],\"e\":\"🌱\"},{\"t\":\"Abióticos\",\"d\":\"(medio "
    "físico)\",\"items\":[\"Agua\",\"Aire\"],\"e\":\"☀️\"}]},{\"t\":\"Importancia\",\"enlace\":\"Nos "
    "brindan:\",\"items\":[\"Alimentos\",\"Oxígeno\"],\"e\":\"🌍\"}]}. Reglas: \"titulo\" es el tema; "
    "\"definicion\" es una frase que define el tema (empieza con \"Son:\", \"Es:\", etc.); de 3 a 5 "
    "ramas; cada rama lleva \"enlace\" (frase corta que une la rama con sus tarjetas: \"Se dividen en:\", "
    "\"Pueden ser:\", \"Sus características son:\", \"Nos brindan:\") y, o bien \"hijos\" (de 2 a 3 tarjetas "
    "con \"t\" título, \"d\" descripción breve opcional, \"items\" lista de 2 a 5 viñetas cortas opcionales y "
    "\"e\" un emoji que represente la idea), o bien \"items\" y \"e\" directamente en la rama; todo el "
    "texto debe ser breve, correcto y en español. Solo si piden un mapa de conceptos con muchas "
    "relaciones cruzadas usa el formato de red {\"tipo\":\"mapa-conceptual\",\"titulo\":\"...\","
    "\"relaciones\":[{\"de\":\"Concepto A\",\"r\":\"se divide en\",\"a\":\"Concepto B\"}]}.\n"
    "  * Línea de tiempo: {\"tipo\":\"linea-tiempo\",\"titulo\":\"...\",\"eventos\":[{\"f\":\"1946\","
    "\"t\":\"Suceso\",\"d\":\"detalle breve opcional\"}]}\n"
    "  * Pasos en orden: {\"tipo\":\"proceso\",\"titulo\":\"...\",\"pasos\":[{\"t\":\"Paso\",\"d\":\"detalle "
    "opcional\"}]}\n"
    "  * Ciclo (3 a 8 pasos que se repiten): igual que proceso pero con \"tipo\":\"ciclo\".\n"
    "  * Diagrama de Venn (2 o 3 conjuntos): {\"tipo\":\"venn\",\"titulo\":\"...\",\"conjuntos\":[{\"t\":\"A\","
    "\"items\":[\"rasgo 1\",\"rasgo 2\"]},{\"t\":\"B\",\"items\":[\"...\"]}],\"comun\":[\"rasgo compartido\"]}\n"
    "  * Causa-efecto / espina de pescado: {\"tipo\":\"causa-efecto\",\"titulo\":\"...\",\"efecto\":\"Problema\","
    "\"causas\":[{\"t\":\"Categoría\",\"hijos\":[\"causa 1\",\"causa 2\"]}]} (4 a 6 categorías, hasta 4 "
    "causas cada una).\n"
    "  * DIAGRAMA DE CUERPO LIBRE (DCL): {\"tipo\":\"dcl\",\"titulo\":\"Bloque en plano inclinado\","
    "\"cuerpo\":\"m = 5 kg\",\"superficie\":\"horizontal\"|\"inclinado\"|\"ninguna\",\"angulo\":30,"
    "\"componentes\":true,\"fuerzas\":[{\"t\":\"peso\",\"etiqueta\":\"W = mg\"},{\"t\":\"normal\","
    "\"etiqueta\":\"N\"},{\"t\":\"friccion\",\"sentido\":\"arriba\",\"etiqueta\":\"fr\"},{\"t\":\"tension\","
    "\"ang\":90,\"etiqueta\":\"T\"},{\"t\":\"aplicada\",\"ang\":30,\"etiqueta\":\"F = 50 N\",\"comp\":true}],"
    "\"ecuaciones\":[\"ΣFx = F cos30° − fr = m·a\",\"ΣFy = N + F sin30° − W = 0\"]}. "
    "Reglas del DCL: un diagrama por cuerpo (si hay varios cuerpos, un bloque por cada uno); el tipo de "
    "cada fuerza es peso, normal, friccion, tension, aplicada, empuje o elastica, y la aplicación calcula "
    "las direcciones del peso (vertical hacia abajo), la normal (perpendicular a la superficie) y la "
    "fricción; \"sentido\" de la fricción: en plano inclinado \"arriba\" o \"abajo\" (a lo largo del plano) "
    "y en horizontal \"derecha\" o \"izquierda\", siempre OPUESTO al movimiento o a la tendencia a moverse; "
    "\"ang\" es el ángulo en grados de una fuerza tension/aplicada medido desde el eje +x horizontal hacia "
    "arriba (antihorario), o una de las palabras derecha, izquierda, arriba, abajo, plano-arriba, "
    "plano-abajo; \"angulo\" es la inclinación del plano en grados; con \"componentes\":true en plano "
    "inclinado se dibujan W sinθ y W cosθ; \"comp\":true en una fuerza oblicua dibuja sus componentes; "
    "\"mag\" (0.2 a 1.3) opcional ajusta la longitud relativa de la flecha (en equilibrio vertical, N y W "
    "deben ser iguales). Pon en \"ecuaciones\" las ecuaciones de Newton (ΣFx, ΣFy) con los símbolos del "
    "diagrama. Después del bloque resuelve el problema paso a paso con números y unidades si hay datos, "
    "usando g = 9,8 m/s². No inventes fuerzas que no existan (sin fricción, no dibujes friccion).\n"
    "Reglas de calidad generales: textos de 1 a 4 palabras, detalles de máximo una frase; en mapas "
    "mentales, esquemas y organigramas usa de 4 a 7 ramas principales con 2 a 4 hijos cada una y hasta 3 "
    "niveles; contenido completo, correcto y bien organizado (no dejes ramas vacías ni inventes datos); "
    "\"e\" es un emoji opcional por rama principal. Para comparar cosas usa una tabla en markdown.\n"
    "DIAGRAMAS DE FLUJO: para algoritmos, flujos de decisión, organigramas, arquitectura o mapas "
    "conceptuales con relaciones entre ideas usa un bloque ```mermaid con `flowchart TD` o "
    "`flowchart LR`. Encierra SIEMPRE los textos entre comillas dentro de los corchetes, p. ej. "
    "`A[\"Dióxido de carbono (CO2)\"]`, con textos cortos en español, relaciones con texto como "
    "`A -->|produce| B`, sin punto y coma ni llaves dentro de los textos y un solo diagrama por bloque.\n\n"
    "- Si el usuario envía una imagen, descríbela y, si contiene código o un error, explica qué hace o "
    "qué falla. Si el mensaje incluye texto extraído de un documento adjunto (PDF o Word), úsalo como "
    "contexto principal, comenta en breve de qué trata y luego responde su pregunta puntual.\n"
    "- Responde siempre en español, con tono cercano, claro y con buena actitud."
)

POOL_GEMINI = PoolClaves("Gemini", [], lambda k: None)
POOL_GROQ = PoolClaves("Groq", [], lambda k: None)
GEMINI_DISPONIBLE = False
GROQ_DISPONIBLE = False

_claves_gemini = leer_claves("GEMINI")
if _claves_gemini:
    try:
        from google import genai

        POOL_GEMINI = PoolClaves("Gemini", _claves_gemini, lambda k: genai.Client(api_key=k))
        GEMINI_DISPONIBLE = True
    except Exception:
        GEMINI_DISPONIBLE = False

_claves_groq = leer_claves("GROQ")
if _claves_groq:
    try:
        from groq import Groq

        POOL_GROQ = PoolClaves("Groq", _claves_groq, lambda k: Groq(api_key=k))
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


def traducir_error_ia(error: Exception) -> str:
    """Convierte errores técnicos de Gemini/Groq (JSON larguísimo, códigos
    internos) en un mensaje corto y entendible para el usuario final."""
    texto_error = str(error)
    if "RESOURCE_EXHAUSTED" in texto_error or "429" in texto_error or "quota" in texto_error.lower():
        return (
            "CodIA alcanzó el límite de uso gratuito de la IA por ahora. "
            "Espera un minuto e inténtalo de nuevo (si esto pasa seguido, "
            "puede que tu clave de API esté siendo usada por alguien más, "
            "o que necesites habilitar facturación en Google AI Studio)."
        )
    if "API key" in texto_error or "API_KEY_INVALID" in texto_error or "401" in texto_error:
        return "La clave de API configurada no es válida. Revisa el archivo .env del backend."
    if "timeout" in texto_error.lower() or "timed out" in texto_error.lower():
        return "El servicio de IA tardó demasiado en responder. Intenta de nuevo."
    return "No se pudo obtener respuesta de la IA en este momento. Intenta de nuevo en un momento."


# ---------------------------------------------------------------------------
# Generación de imágenes con varios proveedores y respaldo automático.
# Los modelos de imagen de Gemini suelen NO tener cuota gratuita (error 429
# aunque el chat funcione), así que CodIA prueba en orden y usa el primero
# que responda. Orden por defecto: gemini -> huggingface -> pollinations.
# Cambiar el orden: IMAGE_PROVIDERS=huggingface,pollinations,gemini
# ---------------------------------------------------------------------------
IMAGE_PROVIDERS = [p.strip().lower() for p in os.getenv("IMAGE_PROVIDERS", "gemini,cloudflare,huggingface,pollinations").split(",") if p.strip()]
HF_API_TOKEN = (os.getenv("HF_API_TOKEN") or os.getenv("HF_TOKEN") or "").strip().strip('"').strip("'")
HF_IMAGE_MODEL = os.getenv("HF_IMAGE_MODEL", "black-forest-labs/FLUX.1-schnell")
POLLINATIONS_API_KEY = (os.getenv("POLLINATIONS_API_KEY") or "").strip().strip('"').strip("'")
_pausa_imagen_gemini_hasta = 0.0  # si Gemini dio 429 de imágenes, no insistir un rato


def _validar_respuesta_imagen(r, nombre: str) -> bytes:
    if r.status_code != 200:
        raise RuntimeError(f"{nombre} respondió {r.status_code}: {r.text[:200]}")
    if not (r.headers.get("content-type", "").startswith("image/")) or len(r.content) < 1000:
        raise RuntimeError(f"{nombre} no devolvió una imagen válida")
    return r.content


def _imagen_gemini(prompt: str) -> bytes:
    # penalizar_claves=False: un 429 de IMÁGENES no debe poner en pausa las
    # claves que siguen sirviendo bien para el chat.
    respuesta = POOL_GEMINI.ejecutar(
        lambda cliente: cliente.models.generate_content(model=GEMINI_IMAGE_MODEL, contents=[prompt]),
        penalizar_claves=False,
    )
    for parte in respuesta.candidates[0].content.parts:
        datos_inline = getattr(parte, "inline_data", None)
        if datos_inline and getattr(datos_inline, "data", None):
            return datos_inline.data
    raise ValueError("La respuesta de Gemini no incluyó ninguna imagen.")


CLOUDFLARE_ACCOUNT_ID = (os.getenv("CLOUDFLARE_ACCOUNT_ID") or "").strip().strip('"').strip("'")
CLOUDFLARE_API_TOKEN = (os.getenv("CLOUDFLARE_API_TOKEN") or "").strip().strip('"').strip("'")
CLOUDFLARE_IMAGE_MODEL = os.getenv("CLOUDFLARE_IMAGE_MODEL", "@cf/black-forest-labs/flux-1-schnell")


def _imagen_cloudflare(prompt: str) -> bytes:
    """FLUX.1-schnell en Cloudflare Workers AI (cupo gratuito diario)."""
    import base64

    r = requests.post(
        f"https://api.cloudflare.com/client/v4/accounts/{CLOUDFLARE_ACCOUNT_ID}/ai/run/{CLOUDFLARE_IMAGE_MODEL}",
        headers={"Authorization": f"Bearer {CLOUDFLARE_API_TOKEN}"},
        json={"prompt": prompt[:2000], "steps": 6},
        timeout=120,
    )
    if r.status_code != 200:
        raise RuntimeError(f"Cloudflare respondió {r.status_code}: {r.text[:200]}")
    if r.headers.get("content-type", "").startswith("image/"):
        return r.content
    b64 = (r.json().get("result") or {}).get("image")
    if not b64:
        raise RuntimeError(f"Cloudflare no devolvió imagen: {r.text[:200]}")
    return base64.b64decode(b64)


def _imagen_huggingface(prompt: str) -> bytes:
    r = requests.post(
        f"https://router.huggingface.co/hf-inference/models/{HF_IMAGE_MODEL}",
        headers={"Authorization": f"Bearer {HF_API_TOKEN}", "Accept": "image/png"},
        json={"inputs": prompt},
        timeout=120,
    )
    return _validar_respuesta_imagen(r, "Hugging Face")


def _imagen_pollinations(prompt: str) -> bytes:
    from urllib.parse import quote

    url = "https://image.pollinations.ai/prompt/" + quote(prompt[:800]) + "?width=1024&height=1024&nologo=true&model=flux"
    cabeceras = {"Authorization": f"Bearer {POLLINATIONS_API_KEY}"} if POLLINATIONS_API_KEY else {}
    r = requests.get(url, headers=cabeceras, timeout=120)
    return _validar_respuesta_imagen(r, "Pollinations")


def mejorar_prompt_imagen(pedido: str) -> str:
    """Reescribe el pedido del usuario como un prompt claro en inglés para el
    generador de imágenes. Si algo falla, devuelve el pedido original."""
    instruccion = (
        "Convierte el pedido del usuario en UN prompt en inglés para un generador de imágenes. "
        "Interpreta la intención (aunque venga mal escrito o mal transcrito por voz), describe "
        "sujeto, acción, entorno, iluminación y estilo en una sola frase de máximo 60 palabras. "
        "Mantén exactamente lo que el usuario pidió; no agregues contenido sexual ni violento. "
        "Responde SOLO con el prompt, sin comillas ni explicaciones.\n\nPedido: " + pedido.strip()
    )
    try:
        if GROQ_DISPONIBLE:
            r = POOL_GROQ.ejecutar(
                lambda c: c.chat.completions.create(
                    model=GROQ_CHAT_MODEL,
                    messages=[{"role": "user", "content": instruccion}],
                    temperature=0.4,
                    max_tokens=160,
                )
            )
            texto = (r.choices[0].message.content or "").strip()
        elif GEMINI_DISPONIBLE:
            r = POOL_GEMINI.ejecutar(
                lambda c: c.models.generate_content(model="gemini-flash-latest", contents=instruccion)
            )
            texto = (r.text or "").strip()
        else:
            return pedido
        texto = texto.strip().strip('"').strip()
        return texto[:700] if len(texto) > 8 else pedido
    except Exception as e:  # noqa: BLE001
        print(f"[CodIA] no se pudo mejorar el prompt de imagen: {str(e)[:200]}", flush=True)
        return pedido


def generar_imagen_ia(prompt: str) -> bytes:
    """Genera una imagen probando los proveedores en orden (IMAGE_PROVIDERS).
    Devuelve los bytes de la imagen o lanza HTTPException con un mensaje claro."""
    global _pausa_imagen_gemini_hasta
    import time

    fallos = []
    for nombre in IMAGE_PROVIDERS:
        try:
            if nombre == "gemini":
                if not GEMINI_DISPONIBLE or time.time() < _pausa_imagen_gemini_hasta:
                    continue
                try:
                    return _imagen_gemini(prompt)
                except Exception as e:
                    if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e) or "quota" in str(e).lower():
                        _pausa_imagen_gemini_hasta = time.time() + 1800  # 30 min sin insistir
                    raise
            elif nombre == "cloudflare":
                if not (CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN):
                    continue
                return _imagen_cloudflare(prompt)
            elif nombre == "huggingface":
                if not HF_API_TOKEN:
                    continue
                return _imagen_huggingface(prompt)
            elif nombre == "pollinations":
                return _imagen_pollinations(prompt)
        except Exception as e:  # noqa: BLE001
            fallos.append(nombre)
            print(f"[CodIA] imágenes: {nombre} falló -> {str(e)[:300]}", flush=True)

    if not fallos:
        raise HTTPException(status_code=503, detail="No hay ningún proveedor de imágenes configurado.")
    raise HTTPException(
        status_code=502,
        detail=(
            "No se pudo generar la imagen ahora mismo (los servicios gratuitos de imágenes están "
            "ocupados o sin cupo). Inténtalo de nuevo en unos minutos. "
            f"Probé con: {', '.join(fallos)}."
        ),
    )


def generar_respuesta_ia(
    mensaje_usuario: str,
    historial: List[Mensaje],
    imagen_base64: Optional[str] = None,
    imagen_mime: Optional[str] = None,
):
    """Devuelve (texto_respuesta, proveedor_usado)."""

    ultimo_error = None

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

            respuesta = POOL_GEMINI.ejecutar(
                lambda cliente: cliente.models.generate_content(
                    model="gemini-flash-latest",
                    contents=contenidos,
                    config=types.GenerateContentConfig(system_instruction=INSTRUCCION_SISTEMA),
                )
            )
            return respuesta.text, "gemini"
        except Exception as error_gemini:
            print(f"[CodIA] Error de Gemini: {error_gemini}")
            ultimo_error = error_gemini  # seguimos al plan B

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

            respuesta_groq = POOL_GROQ.ejecutar(
                lambda cliente: cliente.chat.completions.create(
                    model=GROQ_CHAT_MODEL,
                    messages=mensajes_groq,
                    temperature=0.7,
                )
            )
            return respuesta_groq.choices[0].message.content, "groq"
        except Exception as error_groq:
            print(f"[CodIA] Error de Groq: {error_groq}")
            raise HTTPException(status_code=502, detail=traducir_error_ia(error_groq))

    if ultimo_error is not None:
        raise HTTPException(status_code=502, detail=traducir_error_ia(ultimo_error))

    raise HTTPException(
        status_code=503,
        detail="No hay ninguna API de IA configurada. Revisa tu archivo .env",
    )


def generar_respuesta_ia_stream(
    mensaje_usuario: str,
    historial: List[Mensaje],
    imagen_base64: Optional[str] = None,
    imagen_mime: Optional[str] = None,
):
    """Versión en streaming de generar_respuesta_ia: va produciendo (pedazo_de_texto,
    proveedor) a medida que la IA los genera, en vez de esperar la respuesta
    completa. Esto es lo que hace que el texto aparezca palabra por palabra
    en el chat, como en ChatGPT, en vez de aparecer todo de golpe al final.

    El fallback a Groq solo puede pasar si Gemini falla ANTES de mandar el
    primer pedazo — una vez que ya se envió algo al usuario, no se puede
    'deshacer' y cambiar de proveedor a mitad de una respuesta.
    """
    algo_enviado = False

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

            def _pedazos_gemini(cliente):
                flujo = cliente.models.generate_content_stream(
                    model="gemini-flash-latest",
                    contents=contenidos,
                    config=types.GenerateContentConfig(system_instruction=INSTRUCCION_SISTEMA),
                )
                for fragmento in flujo:
                    texto_pedazo = getattr(fragmento, "text", None)
                    if texto_pedazo:
                        yield texto_pedazo

            # El pool prueba otra clave si esta da 429 antes del primer pedazo.
            for texto_pedazo, _i in POOL_GEMINI.ejecutar_stream(_pedazos_gemini):
                algo_enviado = True
                yield texto_pedazo, "gemini"
            if algo_enviado:
                return
        except Exception as error_gemini:
            print(f"[CodIA] Error de Gemini (stream): {error_gemini}")
            if algo_enviado:
                yield "\n\n⚠️ La conexión con la IA se interrumpió a mitad de la respuesta.", "gemini"
                return
            # todavía no se mandó nada al usuario: es seguro caer al plan B

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

            def _pedazos_groq(cliente):
                flujo = cliente.chat.completions.create(
                    model=GROQ_CHAT_MODEL,
                    messages=mensajes_groq,
                    temperature=0.7,
                    stream=True,
                )
                for fragmento in flujo:
                    delta = fragmento.choices[0].delta.content
                    if delta:
                        yield delta

            for delta, _i in POOL_GROQ.ejecutar_stream(_pedazos_groq):
                yield delta, "groq"
            return
        except Exception as error_groq:
            print(f"[CodIA] Error de Groq (stream): {error_groq}")
            yield traducir_error_ia(error_groq), "error"
            return

    yield "No hay ninguna API de IA configurada. Revisa tu archivo .env", "error"


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

# Sirve las imágenes y documentos en /api/imagenes/<archivo>: primero del disco
# (rápido) y, si el disco se borró por un redeploy, desde la base de datos.
import re as _re

_NOMBRE_ARCHIVO_VALIDO = _re.compile(r"^[0-9a-f]{32}\.(png|jpg|webp|pdf|docx)$")


@app.get("/api/imagenes/{nombre}")
def servir_archivo(nombre: str):
    if not _NOMBRE_ARCHIVO_VALIDO.match(nombre):
        raise HTTPException(status_code=404, detail="Archivo no encontrado")
    ruta = os.path.join(UPLOADS_DIR, nombre)
    cabeceras = {"Cache-Control": "public, max-age=31536000, immutable"}
    mime = MIME_POR_EXTENSION.get(nombre.rsplit(".", 1)[-1], "application/octet-stream")
    if os.path.isfile(ruta):
        with open(ruta, "rb") as f:
            return Response(content=f.read(), media_type=mime, headers=cabeceras)
    db = SessionLocal()
    try:
        archivo = db.get(Archivo, nombre)
        if archivo is None:
            raise HTTPException(status_code=404, detail="Archivo no encontrado")
        datos, mime_bd = bytes(archivo.datos), archivo.mime
    finally:
        db.close()
    try:  # lo dejamos otra vez en disco para que la próxima vez sea más rápido
        with open(ruta, "wb") as f:
            f.write(datos)
    except OSError:
        pass
    return Response(content=datos, media_type=mime_bd, headers=cabeceras)


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
        "voz_disponible": GROQ_DISPONIBLE,
        "claves": {"gemini": POOL_GEMINI.resumen(), "groq": POOL_GROQ.resumen()},
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


def _preparar_mensaje_usuario(solicitud: "SolicitudChat", conversacion: Conversacion, db: Session):
    """Lógica compartida entre /api/chat y /api/chat/stream: guarda la
    imagen/documento adjunto en disco, guarda el mensaje del usuario en la
    base de datos, actualiza el título de la conversación si hace falta, y
    devuelve (mensaje_para_ia, historial_previo) listos para pasarle a la IA."""
    imagen_url = None
    if solicitud.imagen_base64:
        try:
            extension = "png" if "png" in (solicitud.imagen_mime or "") else "jpg"
            nombre_archivo = f"{uuid.uuid4().hex}.{extension}"
            guardar_archivo(nombre_archivo, base64.b64decode(solicitud.imagen_base64))
            imagen_url = f"/api/imagenes/{nombre_archivo}"
        except Exception:
            raise HTTPException(status_code=400, detail="La imagen enviada no es válida")

    documento_url = None
    mensaje_para_ia = solicitud.mensaje
    if solicitud.documento_base64:
        try:
            datos_documento = base64.b64decode(solicitud.documento_base64)
            nombre_original = solicitud.documento_nombre or "documento"
            extension = "pdf" if nombre_original.lower().endswith(".pdf") else "docx"
            nombre_archivo = f"{uuid.uuid4().hex}.{extension}"
            guardar_archivo(nombre_archivo, datos_documento)
            documento_url = f"/api/imagenes/{nombre_archivo}"

            texto_extraido = extraer_texto_documento(
                datos_documento, solicitud.documento_mime or "", nombre_original
            )
            mensaje_para_ia = (
                f"{solicitud.mensaje}\n\n[Documento adjunto: {nombre_original}]\n{texto_extraido}"
            )
        except Exception:
            raise HTTPException(status_code=400, detail="El documento enviado no es válido")

    historial_previo = (
        db.query(Mensaje)
        .filter(Mensaje.conversacion_id == conversacion.id)
        .order_by(Mensaje.creado_en.asc())
        .all()
    )

    mensaje_usuario = Mensaje(
        conversacion_id=conversacion.id,
        rol="user",
        contenido=solicitud.mensaje,
        imagen_url=imagen_url,
        documento_url=documento_url,
        documento_nombre=solicitud.documento_nombre if documento_url else None,
    )
    db.add(mensaje_usuario)

    if conversacion.titulo == "Nuevo chat" and solicitud.mensaje.strip():
        titulo = solicitud.mensaje.strip()[:30]
        conversacion.titulo = titulo + ("..." if len(solicitud.mensaje.strip()) > 30 else "")

    db.commit()
    return mensaje_para_ia, historial_previo


@app.post("/api/chat", response_model=RespuestaChat)
def chat(solicitud: SolicitudChat, db: Session = Depends(get_db)):
    if not solicitud.mensaje.strip() and not solicitud.imagen_base64 and not solicitud.documento_base64:
        raise HTTPException(status_code=400, detail="El mensaje no puede estar vacío")

    conversacion = db.get(Conversacion, solicitud.conversacion_id)
    if conversacion is None:
        raise HTTPException(status_code=404, detail="Conversación no encontrada")

    mensaje_para_ia, historial_previo = _preparar_mensaje_usuario(solicitud, conversacion, db)

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


@app.post("/api/chat/stream")
def chat_stream(solicitud: SolicitudChat, db: Session = Depends(get_db)):
    """Igual que /api/chat, pero devuelve la respuesta en pedazos a medida
    que la IA los genera (streaming), en vez de esperar el mensaje completo.
    El frontend lee esto como texto plano en tiempo real."""
    if not solicitud.mensaje.strip() and not solicitud.imagen_base64 and not solicitud.documento_base64:
        raise HTTPException(status_code=400, detail="El mensaje no puede estar vacío")

    conversacion = db.get(Conversacion, solicitud.conversacion_id)
    if conversacion is None:
        raise HTTPException(status_code=404, detail="Conversación no encontrada")

    mensaje_para_ia, historial_previo = _preparar_mensaje_usuario(solicitud, conversacion, db)
    conversacion_id = conversacion.id
    imagen_base64 = solicitud.imagen_base64
    imagen_mime = solicitud.imagen_mime

    def generador():
        texto_completo = ""
        try:
            for pedazo, _proveedor in generar_respuesta_ia_stream(
                mensaje_para_ia, historial_previo, imagen_base64=imagen_base64, imagen_mime=imagen_mime
            ):
                texto_completo += pedazo
                yield pedazo
        finally:
            # Sesión de base de datos propia: este código sigue corriendo
            # después de que termina la función del endpoint, así que no
            # podemos reutilizar la sesión `db` inyectada por FastAPI.
            db_final = SessionLocal()
            try:
                db_final.add(
                    Mensaje(conversacion_id=conversacion_id, rol="assistant", contenido=texto_completo or "")
                )
                db_final.commit()
            finally:
                db_final.close()

    return StreamingResponse(generador(), media_type="text/plain; charset=utf-8")





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
    prompt_mejorado = mejorar_prompt_imagen(solicitud.prompt)
    print(f"[CodIA] prompt de imagen: {prompt_mejorado}", flush=True)
    imagen_bytes = generar_imagen_ia(prompt_mejorado)
    nombre_archivo = f"{uuid.uuid4().hex}.{extension_de_imagen(imagen_bytes)}"
    guardar_archivo(nombre_archivo, imagen_bytes)
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


# Frases que Whisper "inventa" cuando el audio está en silencio, muy bajo o
# cortado (es un defecto conocido del modelo: aprendió de videos de YouTube).
FRASES_ALUCINADAS = {
    "gracias", "muchas gracias", "gracias por ver el video", "gracias por ver",
    "suscribete", "suscribete al canal", "hasta la proxima", "hasta luego",
    "subtitulos realizados por la comunidad de amaraorg", "amaraorg",
    "subtitulado por la comunidad de amaraorg", "gracias por vernos",
}


def _normalizar_texto(texto: str) -> str:
    import unicodedata, re as _r
    t = unicodedata.normalize("NFD", (texto or "").lower())
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    return _r.sub(r"[^a-z0-9 ]", "", t).strip()


def _campo(obj, nombre, defecto=None):
    """Lee un campo tanto de objetos como de diccionarios (según la versión del SDK)."""
    if isinstance(obj, dict):
        return obj.get(nombre, defecto)
    return getattr(obj, nombre, defecto)


def transcribir_audio_ia(datos_audio: bytes, nombre_archivo: str) -> str:
    """Transcribe un audio a texto usando Whisper vía Groq. Se usa para la
    entrada de voz: funciona en cualquier navegador (Brave, Firefox, Safari,
    Chrome) porque solo graba el audio en el navegador y lo manda al
    backend — no depende del servicio de voz de Google.

    Mejoras contra el clásico "gracias." de Whisper: temperatura 0, una pista
    de contexto (español + programación), y se descartan las transcripciones
    donde el propio modelo dice que NO había voz (no_speech_prob alto) o que
    es una frase inventada típica."""
    if not GROQ_DISPONIBLE:
        raise HTTPException(
            status_code=503,
            detail="La transcripción de voz necesita Groq configurado en el backend.",
        )
    if len(datos_audio) < 2000:
        raise HTTPException(status_code=422, detail="El audio es demasiado corto. Mantén pulsado un poco más y habla claro.")
    try:
        resultado = POOL_GROQ.ejecutar(
            lambda cliente: cliente.audio.transcriptions.create(
                file=(nombre_archivo, datos_audio),
                model=GROQ_WHISPER_MODEL,
                language="es",
                temperature=0.0,
                response_format="verbose_json",
                prompt="Conversación en español con un asistente. Se habla de cualquier tema: tareas, ciencias, historia, programación, Python, JavaScript y bases de datos.",
            )
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=traducir_error_ia(e))

    texto = (_campo(resultado, "text", "") or "").strip()
    segmentos = _campo(resultado, "segments", None) or []
    duracion = _campo(resultado, "duration", None)

    prob_sin_voz = None
    logprob = None
    if segmentos:
        probs = [_campo(sg, "no_speech_prob", 0.0) or 0.0 for sg in segmentos]
        logs = [_campo(sg, "avg_logprob", 0.0) or 0.0 for sg in segmentos]
        prob_sin_voz = sum(probs) / len(probs)
        logprob = sum(logs) / len(logs)
    print(f"[CodIA] voz: dur={duracion} sin_voz={prob_sin_voz} logprob={logprob} texto={texto[:60]!r}", flush=True)

    sin_voz = prob_sin_voz is not None and (
        prob_sin_voz > 0.8 or (prob_sin_voz > 0.55 and logprob is not None and logprob < -0.7)
    )
    es_frase_inventada = _normalizar_texto(texto) in FRASES_ALUCINADAS and (
        (prob_sin_voz is not None and prob_sin_voz > 0.25) or (duracion is not None and duracion < 2.5)
    )
    if not texto or sin_voz or es_frase_inventada:
        raise HTTPException(
            status_code=422,
            detail="No se escuchó bien tu voz. Acércate al micrófono, habla un poco más fuerte y revisa que sea el micrófono correcto.",
        )
    return texto


class SolicitudTranscribir(BaseModel):
    audio_base64: str
    nombre_archivo: str = "audio.webm"


class RespuestaTranscribir(BaseModel):
    texto: str


@app.post("/api/transcribir-audio", response_model=RespuestaTranscribir)
def transcribir_audio(solicitud: SolicitudTranscribir):
    if not solicitud.audio_base64:
        raise HTTPException(status_code=400, detail="No se recibió audio")
    try:
        datos_audio = base64.b64decode(solicitud.audio_base64)
    except Exception:
        raise HTTPException(status_code=400, detail="El audio enviado no es válido")
    texto = transcribir_audio_ia(datos_audio, solicitud.nombre_archivo)
    return RespuestaTranscribir(texto=texto)


@app.get("/")
def read_root():
    return {"message": "API de CodIA funcionando correctamente"}
