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
  - *Hablar en vez de escribir:* el botón 🎤 graba tu voz con `MediaRecorder`
    (una API estándar de grabación, soportada en todos los navegadores
    modernos — a diferencia de `SpeechRecognition`, que Brave y Firefox
    bloquean porque depende de un servicio de Google) y manda el audio al
    backend, que lo transcribe con **Whisper a través de Groq**
    (`whisper-large-v3-turbo`, en español). El botón se oculta solo si el
    backend no tiene Groq configurado, o si el navegador no soporta
    grabación. Requiere `https://` o `localhost` — los navegadores no dan
    acceso al micrófono en HTTP plano.
  - *Escuchar las respuestas:* el interruptor "🔊 Voz" en el encabezado
    hace que CodIA lea sus respuestas en voz alta (con `SpeechSynthesis`,
    del navegador — sin costo ni API externa). También hay un botón 🔊 en
    cada burbuja para releer un mensaje puntual.
  - *Para cuando compiles la app nativa (Android/iOS):* `getUserMedia`
    (usado por `MediaRecorder`) sí funciona dentro del WebView de
    Capacitor, pero necesita permisos nativos declarados. En Android,
    agrega `<uses-permission android:name="android.permission.RECORD_AUDIO"/>`
    a `mobile/android/app/src/main/AndroidManifest.xml` después de correr
    `npx cap add android`. En iOS, agrega la clave `NSMicrophoneUsageDescription`
    (con un texto explicando para qué se usa) a `mobile/ios/App/App/Info.plist`
    después de `npx cap add ios`. Sin esto, el permiso se deniega en
    silencio y el botón no hará nada dentro de la app compilada.

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

Verifica que funciona abriendo `http://127.0.0.1:8000/docs` (documentación
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

1. **Backend en Render** — dos formas, elige una:
   - **Con Blueprint (recomendado, evita errores de configuración):** en
     Render, "New +" → "Blueprint" → conecta tu repo de GitHub. Render lee
     el archivo `render.yaml` de la raíz del proyecto y configura solo la
     carpeta (`backend/`), el build y el comando de arranque. Solo te va a
     pedir que pegues `GEMINI_API_KEY` y `GROQ_API_KEY` la primera vez.
   - **Manual:** "New +" → "Web Service" → conecta el repo → configura a mano:
     - **Root Directory:** `backend`
     - **Build Command:** `pip install -r requirements.txt`
     - **Start Command:** `uvicorn app:app --host 0.0.0.0 --port $PORT`
       (⚠️ usa `$PORT`, no `8000` — Render asigna el puerto dinámicamente;
       con un puerto fijo el servicio nunca llega a responder)
     - **Environment → Add Environment Variable:** `GEMINI_API_KEY` y
       `GROQ_API_KEY` con tus claves (nunca las subas en `.env` al repo —
       tu `.gitignore` ya lo excluye).
   - **⚠️ Disco efímero en el plan gratuito:** Render *free tier* no
     conserva archivos entre reinicios/redeploys. Eso significa que
     `backend/codia.db` (tus conversaciones) y `backend/uploads/`
     (imágenes/documentos subidos) **se borran cada vez que Render
     reinicia el servicio** (duerme tras 15 min sin uso, o si haces un
     nuevo deploy). Para desarrollo/demo está bien; para producción real,
     lo siguiente sería migrar a una base de datos externa persistente
     (ej. el Postgres gratuito de Render) — solo cambiarías `DATABASE_URL`
     en `app.py`, no la lógica.
2. **Frontend:** despliega la carpeta `frontend/` en Vercel, Netlify o
   GitHub Pages (cualquiera te da HTTPS gratis, necesario para que la PWA
   sea instalable).
3. En `frontend/index.html`, cambia la constante `API_URL` (o define
   `window.CODIA_API_URL` antes de cargar el script) para que apunte a la
   URL pública de tu backend en vez de `127.0.0.1:8000`.

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

## Novedades v5: respuestas en streaming

- El chat ahora usa `/api/chat/stream` en vez de esperar la respuesta
  completa: el texto de CodIA va apareciendo palabra por palabra a medida
  que la IA lo genera, como en ChatGPT, en vez de aparecer todo de golpe
  al final. Mientras llega, se muestra como texto plano; en cuanto termina,
  se reemplaza por la versión con formato completo (bloques de código con
  sus botones, diagramas Mermaid, etc.), porque esas cosas necesitan el
  texto completo para reconocer dónde empiezan y terminan.
- Si Gemini fallara a mitad de una respuesta ya empezada a mostrar, no hay
  forma de "deshacerla" y cambiar a Groq sin que se vea raro — en ese caso
  el chat simplemente avisa que la conexión se interrumpió, en vez de
  perder silenciosamente lo que ya se había mostrado.
- El endpoint anterior sin streaming (`/api/chat`) se mantiene tal cual,
  por si en el futuro construyes otro cliente (por ejemplo, un plugin de
  terceros) que prefiera recibir la respuesta completa de una sola vez.

## Qué extendería primero

1. **Confirmar los permisos nativos de micrófono** una vez compiles para
   Android/iOS (ver la sección de Voz más arriba) — el mecanismo en sí
   (grabar + transcribir con Whisper) ya funciona en cualquier navegador,
   solo falta declarar el permiso nativo en cada plataforma.
2. **Autenticación real de usuarios** — ahora mismo el "usuario" es solo un
   UUID anónimo generado en el dispositivo; si alguien borra el
   `localStorage` o cambia de celular, pierde el acceso a su historial
   (aunque los datos siguen en la base de datos). Agregar login (aunque
   sea simple, con Firebase Auth) permitiría recuperar el historial desde
   cualquier dispositivo.
3. **Rate limiting / control de costos** en el backend, para que la API de
   Gemini/Groq (generación de imágenes incluida) no se pueda saturar si el
   proyecto se comparte públicamente (por ejemplo con `slowapi`).
