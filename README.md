# Agente de fotos

Agente de consola que busca y descarga imágenes de cualquier tema usando Gemini
(para interpretar el pedido) y DuckDuckGo / Wikimedia Commons (para encontrar y
descargar las imágenes).

## Requisitos

- Python 3.10 o superior
- Una API key de Gemini (https://aistudio.google.com/apikey)

## Instalación

```powershell
# 1. Crear y activar un entorno virtual (opcional pero recomendado)
python -m venv .venv
.venv\Scripts\Activate.ps1

# 2. Instalar las dependencias
pip install -r requirements.txt
```

## Configurar la GEMINI_API_KEY

Creá un archivo `.env` en la raíz del proyecto con esta línea:

```env
GEMINI_API_KEY=tu_clave_de_gemini
```

El archivo `.env` está incluido en `.gitignore`, así que tu clave nunca se sube
al repositorio.

## Uso

```powershell
python agente_fotos.py
```

Escribí un pedido en lenguaje natural, por ejemplo:

```text
buscá 4 fotos de petunias y ponelas en Petunias
```

Las imágenes se guardan en la carpeta indicada (o en una con el nombre del tema
si no especificás ninguna). Escribí `salir` para terminar.
