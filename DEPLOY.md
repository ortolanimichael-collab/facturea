# Cómo poner Facturea en producción

Este proyecto está armado para que sea **el mismo código** en dos lugares
distintos: Render (para probar, gratis o casi) y un VPS (para producción de
verdad, cuando ya tengas los clientes listos). No hay que mantener dos
versiones -- lo único que cambia son las variables de entorno.

---

## 1. Google Cloud Console (necesario para los dos lugares)

Esto lo tenés que hacer una sola vez, es lo único que no puedo hacer yo por vos.

1. Entrá a https://console.cloud.google.com/ y creá un proyecto nuevo (ej: "Facturea").
2. En el buscador de arriba, buscá **"Google Drive API"** y apretá **Habilitar**.
3. Andá a **"APIs y servicios" -> "Pantalla de consentimiento OAuth"**:
   - Tipo de usuario: **Externo**.
   - Completá nombre de la app ("Facturea"), tu email de contacto.
   - En "Permisos", agregá el scope **`.../auth/drive.file`** (y `email`, `openid` si te los pide por separado).
   - Guardá. Mientras esté en modo "Prueba" (Testing), vas a tener que agregar a mano el email de cada cliente que vaya a probar la conexión, en la sección "Usuarios de prueba" -- hasta que publiques la app de verdad (paso de "Publicar app"), que pide una verificación básica de Google (no la pesada, esta no lleva días de espera normalmente).
4. Andá a **"Credenciales" -> "Crear credenciales" -> "ID de cliente de OAuth"**:
   - Tipo de aplicación: **Aplicación web**.
   - En "URI de redireccionamiento autorizados", agregá **las dos** (una por cada lugar donde vas a probar):
     - `https://tu-app.onrender.com/google-drive/callback` (Render)
     - `https://tu-dominio-real.com/google-drive/callback` (tu VPS, cuando lo tengas)
   - Guardá y copiá el **Client ID** y el **Client Secret** -- van a las variables `GOOGLE_OAUTH_CLIENT_ID` y `GOOGLE_OAUTH_CLIENT_SECRET`.

---

## 2. Probar en Render

1. Subí este proyecto a un repositorio de GitHub (si todavía no lo hiciste).
2. En Render: **New -> Blueprint**, elegí el repo. Render va a leer `render.yaml` solo y armar el servicio web + la base de datos.
3. Las variables marcadas `sync: false` en `render.yaml` (las de Google, `ADMIN_EMAIL`, `ADMIN_PASSWORD`) las cargás a mano en el panel de Render, en la pestaña "Environment" del servicio.
4. Para `GOOGLE_OAUTH_REDIRECT_URI`, usá la URL que te dio Render: `https://tu-app.onrender.com/google-drive/callback`.
5. **Ojo con la base de datos gratis de Render: expira a los 30 días.** Sirve perfecto para probar, pero no dejes ahí datos de clientes reales por mucho tiempo -- es solo para testear que todo funcione antes de pasar al VPS.

---

## 3. Pasar al VPS (cuando ya tengas los clientes listos)

1. Contratá el VPS (recomendación: Hostinger KVM 2 o similar, con Docker instalable).
2. Instalá Docker y Docker Compose en el VPS (la mayoría de los proveedores lo tienen como opción al crear el servidor, o se instala con un par de comandos).
3. Subí el proyecto al VPS (`git clone` de tu repo, o subiendo el zip).
4. Copiá `.env.example` a `.env` y completá TODOS los valores reales (contraseñas, `SECRET_KEY`, `ENCRYPTION_KEY` generados de verdad, credenciales de Google, y esta vez `GOOGLE_OAUTH_REDIRECT_URI` con tu dominio real).
5. Apuntá tu dominio (comprado en Hostinger, NIC Argentina, o donde sea) a la IP del VPS.
6. Corré:
   ```
   docker compose up -d --build
   ```
   Esto levanta Postgres + la app juntos, aplica las migraciones solo, y te deja el sitio corriendo.
7. Para HTTPS (obligatorio, vas a manejar contraseñas de clientes), lo más simple es poner un proxy como Caddy o Nginx + Certbot delante del contenedor -- avisame cuando llegues a este paso y lo armamos juntos, depende un poco de qué proveedor de VPS elijas al final.

---

## 4. Generar `SECRET_KEY` y `ENCRYPTION_KEY` de verdad

Nunca los dejes en los valores de desarrollo. Se generan así:

```
python3 -c "import secrets; print(secrets.token_hex(32))"
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

El primero es el `SECRET_KEY`, el segundo el `ENCRYPTION_KEY` (el que protege las Claves Fiscales cifradas -- si lo perdés, no se pueden volver a descifrar).

---

## 5. Cada vez que cambies el modelo de datos

Ya NO se borra la base. Se hace así:

```
flask db migrate -m "descripción corta del cambio"
flask db upgrade
```

En el VPS, la migración se aplica sola cada vez que reiniciás el contenedor (`entrypoint.sh` la corre antes de levantar el servidor).
