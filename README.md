# scrob-export

Pequeña web que consulta la API de [Scrob](https://github.com/ellite/scrob) y copia al portapapeles resúmenes de texto (en curso, historial, una serie, estrenos) listos para pegar en una IA y conversar sin spoilers.

Solo usa la librería estándar de Python; no necesita dependencias.

## Bloques

| Pestaña     | Qué copia |
| ----------- | --------- |
| En curso    | Series con episodios por ver: último episodio visto (T/E, título, fecha) y progreso. Las terminadas se omiten. «Próximo a ver» es opcional (puede dar spoilers). |
| Historial   | Rango de fechas (atajos o desde/hasta). Formato compacto (por serie) o detallado (por día). |
| Una serie   | Lo visto de una serie o película, con sugerencias mientras escribes. Sin elegir sugerencia, trae todo lo que coincida. |
| Estrenos    | Ayer, hoy, mañana, 7 o 14 días (calendario de Scrob). |
| Paquete     | Encabezado anti-spoilers (editable) + los bloques que marques en un solo copiado. |
| Diagnóstico | Respuesta cruda (recortada) de cada endpoint de Scrob, para depurar. |

## Variables de entorno

| Variable         | Obligatoria | Default | Descripción |
| ---------------- | :---------: | ------- | ----------- |
| `SCROB_URL`      | Sí          | —       | URL base de Scrob (sin barra final) |
| `SCROB_USERNAME` | Sí          | —       | Usuario de Scrob |
| `SCROB_PASSWORD` | Sí          | —       | Contraseña de Scrob |
| `HOST_PORT`      | No          | `7329`  | Puerto externo publicado por Docker |
| `TZ`             | No          | `UTC`   | Zona horaria (ej. `America/El_Salvador`) para fechas y rangos |
| `MAX_PAGES`      | No          | `200`   | Tope de páginas de historial (×100 eventos) |
| `CACHE_TTL`      | No          | `60`    | Segundos de caché por consulta |

## Despliegue

### Docker Compose

```bash
cp .env.example .env   # editar valores
docker compose up -d --build
```

### Portainer (desde repositorio)

1. **Stacks → Add stack → Repository** y apunta al repo (rama y `docker-compose.yml`).
2. En **Environment variables** define `SCROB_URL`, `SCROB_USERNAME`, `SCROB_PASSWORD` (y opcionalmente `HOST_PORT`, `TZ`).
3. Deploy. Tras cada `push`, usa **Pull and redeploy** y asegúrate de que la imagen se reconstruye (el compose usa `build: .`).

Comprobación: `http://<host>:<HOST_PORT>/health` → `{"ok": true}`.

## Notas

- **Se recomienda servirlo detrás de HTTPS**: sin él el navegador bloquea el acceso al portapapeles.
- La app no tiene autenticación propia: no la expongas a Internet. Úsala solo en una red privada o VPN.
- Autenticación contra Scrob: login web (formulario) + cookie de sesión; se renueva sola si caduca. Si cambias la contraseña de Scrob, actualiza `SCROB_PASSWORD`.
- El OpenAPI de Scrob no describe el formato de sus respuestas, por lo que el lector es tolerante. Si un bloque sale con campos vacíos, revisa la pestaña **Diagnóstico**.
- La numeración T/E sigue a TMDB; en anime puede diferir de otras fuentes.
