# CodIA - App Multiplataforma

Convierte el prototipo de chatbox en una app real que funciona igual en
Android, iOS, Windows, Mac y Linux, usando **un solo backend** y un
**frontend web instalable (PWA)**.

## ¿Por qué este stack?

- **Backend: FastAPI (Python)** — reutiliza exactamente la misma lógica de
  IA que ya tenías en `chatbox_streamlit.py` (Gemini primero, Groq de
  respaldo), pero expuesta como una API REST simple (`/api/chat`). Así
  cualquier pantalla puede hablarle al mismo "cerebro".
- **Frontend: PWA (HTML/CSS/JS, tu mismo diseño)** — una Progressive Web
  App es la forma más simple de lograr *multiplataforma de verdad* sin
  aprender Flutter/React Native ni pagar cuentas de desarrollador: el
  usuario abre un link y elige "Instalar app" (o "Agregar a pantalla de
  inicio" en iOS), y queda con un ícono, pantalla completa y la app
  funcionando como una app nativa, en cualquier dispositivo con navegador.

## Estructura

```
codia-app/
├── backend/
│   ├── app.py            # API FastAPI (Gemini + Groq)
│   ├── requirements.txt
│   └── .env.example      # copia esto a .env con tus claves
├── frontend/
│   ├── index.html         # la app (mismo diseño de tu chatbox.html)
│   ├── manifest.json       # hace la app instalable como PWA
│   ├── sw.js                # service worker (shell offline)
│   └── icon.svg
└── mobile/                 # envoltorio Capacitor -> apps nativas Android/iOS
    ├── package.json
    ├── capacitor.config.json
    └── www/                 # copia del frontend usada para compilar el .apk/.ipa
```

## Novedades v4: generar imágenes, leer documentos y voz

- **Generar imágenes:** el botón 🎨 activa el "modo generar imagen" — escribes
  una descripción y CodIA la crea con Gemini (modelo configurable en
  `GEMINI_IMAGE_MODEL` dentro de `.env`, por si Google cambia el nombre del
  modelo con el tiempo). Solo funciona con Gemini; Groq no genera imágenes.
- **Leer documentos:** el botón 📄 deja adjuntar un PDF o un Word (.docx).
  El backend extrae el texto (con `pypdf` y `python-docx`) y se lo pasa a
  la IA como contexto antes de tu pregunta — útil para pedirle que resuma
  un enunciado de tarea o revise un documento de código pegado en Word.
  Los documentos muy largos se recortan a ~6000 caracteres para no gastar
  de más en tokens.
- **Voz:**
  - *Hablar en vez de escribir:* el botón 🎤 usa el reconocimiento de voz
    del navegador (Web Speech API) y transcribe lo que dices al campo de
    texto. Funciona bien en Chrome (Android y escritorio); Firefox no lo
    soporta y el botón se oculta solo. **En la app nativa (Capacitor)
    puede no funcionar dentro del WebView** — para voz confiable en
    Android/iOS empaquetados, lo próximo sería integrar el plugin
    [`@capacitor-community/speech-recognition`](https://github.com/capacitor-community/speech-recognition),
    que si pide permisos nativos de micrófono correctamente.
  - *Escuchar las respuestas:* el interruptor "🔊 Voz" en el encabezado
    hace que CodIA lea sus respuestas en voz alta (con `SpeechSynthesis`,
    también del navegador — sin costo ni API externa). También hay un
    botón 🔊 en cada burbuja para releer un mensaje puntual.

## Novedades v3: auto-depuración y diagramas de flujo

- **Auto-depuración con un clic:** si al presionar "▶ Ejecutar" el código
  falla, aparece un botón "🩹 Corregir con IA" justo debajo del error. Al
  presionarlo, se le manda automáticamente a CodIA el código + el error
  real de la ejecución, pidiéndole que lo corrija y explique en 1-2 frases
  qué estaba mal — cierra el ciclo escribir → ejecutar → fallar → corregir
  sin que el estudiante tenga que copiar/pegar nada manualmente.
- **Diagramas de flujo automáticos:** la IA ahora puede generar bloques
  ```mermaid``` cuando explicar un algoritmo con un diagrama ayuda más que
  puro texto (por ejemplo, para un `if/else` anidado o un bucle). El
  frontend los detecta igual que a los bloques de código, pero en vez de
  mostrar un botón "Ejecutar" los dibuja como un diagrama real usando
  [Mermaid.js](https://mermaid.js.org/) (cargado desde CDN).

## Novedades v2: base de datos, imágenes y ejecución de código

- **Base de datos:** el backend ahora usa SQLite (vía SQLAlchemy) en
  `backend/codia.db`, creado automáticamente la primera vez que corres el
  servidor. Cada dispositivo genera un identificador anónimo (guardado en
  `localStorage`) y sus conversaciones/mensajes se guardan de verdad en la
  base de datos — ya no dependen solo del navegador. La barra lateral
  (como la que tenías en la versión Streamlit) lista esas conversaciones,
  permite crear "Nuevo chat" y borrarlas.
  ⚠️ Si despliegas en un host con disco *no persistente* (algunos planes
  gratuitos de Render, por ejemplo), el archivo `.db` se puede borrar en
  cada redeploy — para producción real conviene migrar a Postgres
  (cambiar solo `DATABASE_URL` en `app.py`, el resto del código no cambia).
- **Imágenes:** el botón 📎 deja adjuntar una foto (por ejemplo, un
  pantallazo de un error o de código) que Gemini analiza directamente
  (es multimodal). Las imágenes se guardan en `backend/uploads/` y se
  sirven en `/api/imagenes/<archivo>`. El respaldo Groq/Llama es solo de
  texto, así que si Gemini falla y cae a Groq, avisa que no pudo ver la
  imagen.
- **Ejecutar código (la parte "innovadora" para una IA de código):** cuando
  CodIA sugiere un bloque de código, el frontend lo detecta automáticamente
  y agrega botones "📋 Copiar" y "▶ Ejecutar". Ejecutar corre el código de
  verdad usando [Piston](https://github.com/engineer-man/piston), una API
  pública y gratuita de sandboxing (sin necesidad de clave), y muestra la
  salida (o el error) justo debajo del bloque — el estudiante puede probar
  lo que la IA propone sin salir de la app ni instalar nada.

## Cómo ejecutarlo localmente

### 1. Backend

```bash
cd backend
python -m venv venv
source venv/bin/activate        # en Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env            # y pega tus claves de Gemini/Groq dentro
uvicorn app:app --reload --host 0.0.0.0 --port 8000
```

Verifica que funciona abriendo `http://localhost:8000/docs` (documentación
automática de la API).

### 2. Frontend

No necesita build ni instalación: es HTML/CSS/JS puro. Solo sírvelo con
cualquier servidor estático (abrir el archivo directamente con `file://`
no permite el service worker, así que usa un servidor simple):

```bash
cd frontend
python -m http.server 5500
```

Abre `http://localhost:5500` en el navegador. Deberías ver el chat
funcionando y conectado al backend (el indicador bajo el encabezado dice
"● Conectado a CodIA").

## Cómo instalarla como app

Con el frontend servido desde una URL con **HTTPS** (requisito de las PWA
fuera de `localhost`):

- **Android (Chrome):** menú ⋮ → "Instalar app" o "Agregar a pantalla de inicio".
- **iPhone (Safari):** botón compartir → "Agregar a pantalla de inicio".
- **Windows/Mac/Linux (Chrome/Edge):** ícono de instalar (⊕) en la barra de direcciones.

## Cómo desplegarla para que cualquiera la use (no solo tú en localhost)

1. **Backend:** despliega `backend/` en un servicio gratuito como Render,
   Railway o Fly.io. Configura ahí las variables de entorno
   `GEMINI_API_KEY` y `GROQ_API_KEY` (nunca subas tu `.env` a GitHub — tu
   `_gitignore` ya lo excluye).
2. **Frontend:** despliega la carpeta `frontend/` en Vercel, Netlify o
   GitHub Pages (cualquiera te da HTTPS gratis, necesario para que la PWA
   sea instalable).
3. En `frontend/index.html`, cambia la constante `API_URL` (o define
   `window.CODIA_API_URL` antes de cargar el script) para que apunte a la
   URL pública de tu backend en vez de `localhost:8000`.

## Publicarla en Google Play Store y Apple App Store

Para que aparezca como una app real e instalable desde las tiendas, se
envuelve el mismo frontend con **Capacitor** (carpeta `mobile/`), que
genera un proyecto Android nativo y un proyecto Xcode nativo a partir de
tu misma app web.

⚠️ **Requisito importante:** compilar el `.apk` necesita Android Studio, y
compilar el `.ipa` necesita una **Mac con Xcode** (Apple no permite
compilar para iOS desde Windows/Linux). Estos pasos debes correrlos en tu
propia computadora — no se pueden ejecutar en este chat.

### 1. Antes de compilar

1. Despliega el `backend/` en un servicio con HTTPS (Render, Railway,
   Fly.io) — ver sección anterior.
2. En `mobile/www/index.html`, reemplaza `API_URL` por la URL pública real
   de ese backend.
3. Ten a mano un ícono cuadrado de tu app en **1024×1024px PNG** (puedes
   partir del `icon.svg` incluido, exportado a PNG con cualquier editor).

### 2. Generar los proyectos nativos

```bash
cd mobile
npm install
npx cap init            # si pregunta, usa los valores de capacitor.config.json
npx cap add android
npx cap add ios         # solo funciona en macOS
npx capacitor-assets generate --iconBackgroundColor '#2575fc' --icon tu-icono-1024.png
npx cap sync
```

Esto crea las carpetas `mobile/android/` y `mobile/ios/` con proyectos
Android Studio / Xcode completos y listos para abrir.

### 3. Compilar y probar

- **Android:** `npx cap open android` → se abre Android Studio → botón
  ▶ para probar en un emulador o celular conectado por USB. Cuando estés
  listo para publicar: `Build → Generate Signed Bundle/APK` (crea primero
  un *keystore*, guárdalo con mucho cuidado, lo necesitas para cada
  actualización futura).
- **iOS:** `npx cap open ios` → se abre Xcode → necesitas una cuenta de
  Apple Developer para firmar el build, incluso para probarlo en un
  iPhone físico.

### 4. Publicar

| | Google Play | Apple App Store |
|---|---|---|
| Costo | $25 USD pago único | $99 USD/año |
| Cuenta | [play.google.com/console](https://play.google.com/console) | [developer.apple.com](https://developer.apple.com) |
| Formato | `.aab` (Android App Bundle) | subir con Xcode/Transporter a App Store Connect |
| Revisión | Horas a ~2 días | Días (puede pedir cambios) |
| Obligatorio | Política de privacidad (URL pública), cuestionario de clasificación de contenido | "App Privacy" (qué datos recopila), a veces piden una cuenta de prueba |

Como CodIA envía los mensajes del usuario a Gemini/Groq, ambas tiendas
te van a pedir una **política de privacidad** explicando eso — puede ser
una página sencilla (una más para desplegar junto al frontend).

## Qué extendería primero

1. **Voz nativa real en Android/iOS** — el micrófono actual usa la API del
   navegador, que dentro del WebView de una app compilada con Capacitor no
   siempre es confiable. Integrar `@capacitor-community/speech-recognition`
   (y el plugin equivalente de texto-a-voz nativo) daría una experiencia
   de voz consistente una vez publicada en las tiendas.
2. **Autenticación real de usuarios** — ahora mismo el "usuario" es solo un
   UUID anónimo generado en el dispositivo; si alguien borra el
   `localStorage` o cambia de celular, pierde el acceso a su historial
   (aunque los datos siguen en la base de datos). Agregar login (aunque
   sea simple, con Firebase Auth) permitiría recuperar el historial desde
   cualquier dispositivo.
3. **Streaming de la respuesta** (que el texto aparezca palabra por
   palabra, como en ChatGPT) en vez de esperar la respuesta completa.
4. **Rate limiting / control de costos** en el backend, para que la API de
   Gemini/Groq (generación de imágenes incluida) no se pueda saturar si el
   proyecto se comparte públicamente (por ejemplo con `slowapi`).
