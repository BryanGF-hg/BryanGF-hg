# 🗺️ WPS en tu perfil de GitHub (sin servidor)

> **TL;DR** — Un SVG animado (mapa ASCII amarillo, paquetes viajando de GitHub a
> Valencia y ondas de luz) que una GitHub Action regenera cada 3 horas con el tráfico
> de tus repos. Nada que hospedar: ni VPS, ni túneles, ni systemd.

## Cómo leer esto

1. [Qué se ve](#qué-se-ve) · 2. [Instalación en 5 pasos](#instalación-en-5-pasos) ·
3. [Límites honestos](#límites-honestos)

## Qué se ve

```
 GitHub Action (cada 3 h)                        README de tu perfil
 ┌───────────────────────────┐                   ┌──────────────────────────────┐
 │ API de tráfico (recuentos)│── profile_svg.py ─►│ wps-map.svg                  │
 │ token en un secret        │   commit si cambia │  * GIT > NYC > MAD > VLC     │
 └───────────────────────────┘                   │  ◯ ondas desde Valencia      │
                                                 │  zona más clara = + visitas  │
                                                 │ [VALENCIA HOY] [14 DÍAS] ... │
                                                 └──────────────────────────────┘
```

| Visitas hoy | Paquetes animados | Ondas | Brillo de Valencia |
|-------------|-------------------|-------|--------------------|
| 0           | 1                 | 1     | mínimo             |
| 1-5         | 1-2               | 2     | ▒                  |
| 6-20        | 3-7               | 3     | ▒▒                 |
| 21-50       | 8                 | 4     | ▒▒▒                |
| 51+         | 8                 | 5     | ▒▒▒▒               |

Es **seguro por diseño**: GitHub nunca ejecuta `<script>` dentro de un SVG de un README;
aquí solo hay animaciones SVG (SMIL), que sí se reproducen.

## Instalación en 5 pasos

1. **Token** — GitHub → Settings → Developer settings → Fine-grained tokens → nuevo token
   con acceso a tus repos y permiso de repositorio **Administration: Read-only**.
2. **Secret** — en tu repo `USUARIO/USUARIO`: Settings → Secrets and variables → Actions →
   *New repository secret* → nombre `WPS_GITHUB_TOKEN`, valor el token.
3. **Copiar archivos** (desde la carpeta del proyecto):
   ```bash
   git clone https://github.com/USUARIO/USUARIO ~/perfil
   ./github-profile/install.sh ~/perfil
   ```
4. **README** — pega en `~/perfil/README.md`:
   ```html
   <p align="center">
     <img src="./wps-map.svg" alt="World Packet Simulator: tráfico de mis repos" width="100%">
   </p>
   ```
   y sube: `cd ~/perfil && git add -A && git commit -m "WPS map" && git push`
5. **Primera ejecución** — pestaña *Actions* → *WPS map* → *Run workflow*. Después se
   actualiza solo cada 3 horas.

Quieres solo algunos repos o otra ciudad: edita `WPS_GITHUB_REPOS` / `WPS_GITHUB_HOME` en
`.github/workflows/wps-map.yml`.

## Límites honestos

* **Visitas a repos, no a tu perfil**: la API de tráfico cuenta las visitas a cada
  repositorio. Las visitas a tu página de perfil no las expone GitHub.
* **No es en directo**: el SVG cambia cuando corre la Action (cada 3 h) y GitHub publica el
  tráfico con algo de retraso. La animación sí se mueve siempre.
* **Anónimo**: solo recuentos (visitas / únicos). Ni IPs, ni nombres, ni ubicaciones.
* Si la API falla, la Action falla **sin** sobrescribir el SVG bueno, y solo hace commit
  cuando cambian los datos (no por la hora).
