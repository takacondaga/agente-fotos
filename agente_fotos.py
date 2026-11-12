"""Agente de consola que descarga imágenes de cualquier tema usando Gemini + DuckDuckGo."""

import inspect
import io
import json
import os
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path

import requests
from ddgs import DDGS
from dotenv import load_dotenv
from google import genai
from google.genai import types
from google.genai.chats import Chat
from PIL import Image

# Carga las variables del archivo .env (por ejemplo GEMINI_API_KEY).
load_dotenv()

MODELO = "gemini-3.8-flash"
MODELO_RESPALDO = "gemini-3.8-flash-lite"
ESPERA_REINTENTO_503 = 2  # segundos
MAX_FOTOS = 50
TIMEOUT_DESCARGA = 20  # segundos (búsquedas de URLs)
TIMEOUT_IMAGEN = 3  # segundos máximo por imagen (si tarda, se descarta)
TAMANIO_MAXIMO = 10 * 1024 * 1024  # 10 MB por imagen

# User-Agent de navegador para que los sitios no bloqueen la descarga.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
    "Accept-Language": "es-AR,es;q=0.9,en;q=0.8",
    "Referer": "https://duckduckgo.com/",
}

# API gratuita de Wikimedia Commons (respaldo cuando DuckDuckGo falla).
WIKIMEDIA_API = "https://commons.wikimedia.org/w/api.php"
WIKIMEDIA_HEADERS = {
    "User-Agent": (
        "AgenteFotos/1.0 (agente de consola para descargar imágenes de cualquier tema; "
        "contacto: agente-fotos@example.com) Python-urllib"
    ),
    "Accept": "application/json",
}


def _descargar_imagen(url: str) -> Image.Image | None:
    """Descarga una imagen y la valida con PIL. Devuelve None si no sirve."""
    try:
        # stream=True + with: el cuerpo se lee por partes y la conexión se
        # cierra apenas se sale del bloque (sin quedarse esperando al servidor).
        with requests.get(
            url, headers=HEADERS, timeout=TIMEOUT_IMAGEN, stream=True
        ) as respuesta:
            respuesta.raise_for_status()

            tamano_declarado = respuesta.headers.get("Content-Length")
            if tamano_declarado:
                try:
                    if int(tamano_declarado) > TAMANIO_MAXIMO:
                        return None
                except ValueError:
                    pass

            trozos: list[bytes] = []
            total = 0
            for trozo in respuesta.iter_content(chunk_size=64 * 1024):
                if not trozo:
                    continue
                total += len(trozo)
                if total > TAMANIO_MAXIMO:
                    return None  # demasiado grande: cortamos la descarga
                trozos.append(trozo)
            datos = b"".join(trozos)
    except requests.RequestException:
        return None  # URL rota, bloqueada, time out, etc.

    if not datos:
        return None

    # verify() detecta archivos corruptos o que no son imágenes.
    prueba = Image.open(io.BytesIO(datos))
    prueba.verify()

    imagen = Image.open(io.BytesIO(datos))
    if imagen.width < 200 or imagen.height < 200:
        return None
    return imagen


def _buscar_en_ddgs(consulta: str, cantidad: int) -> list[str]:
    """Busca imágenes en DuckDuckGo y devuelve sus URLs.

    Lanza la excepción original (por ejemplo un Ratelimit) para que el
    llamador pueda recurrir a otra fuente.
    """
    urls: list[str] = []
    with DDGS() as ddgs:
        for resultado in ddgs.images(consulta, max_results=cantidad * 3) or []:
            url = resultado.get("image")
            if url:
                urls.append(url)
    return urls


def _buscar_en_wikimedia(consulta: str, cantidad: int) -> list[str]:
    """Busca imágenes en Wikimedia Commons y devuelve sus URLs."""
    parametros = {
        "action": "query",
        "generator": "search",
        "gsrsearch": consulta,
        "gsrnamespace": "6",
        "gsrlimit": str(min(max(cantidad * 3, 1), 50)),
        "prop": "imageinfo",
        "iiprop": "url",
        "iiurlwidth": "1200",
        "format": "json",
        "formatversion": "2",
    }
    url = f"{WIKIMEDIA_API}?{urllib.parse.urlencode(parametros)}"
    pedido = urllib.request.Request(url, headers=WIKIMEDIA_HEADERS)

    with urllib.request.urlopen(pedido, timeout=TIMEOUT_DESCARGA) as respuesta:
        datos = json.load(respuesta)

    urls: list[str] = []
    for pagina in (datos.get("query") or {}).get("pages") or []:
        info = (pagina.get("imageinfo") or [{}])[0]
        url_imagen = info.get("thumburl") or info.get("url")
        if url_imagen:
            urls.append(url_imagen)
    return urls


def _singularizar_palabra(palabra: str) -> str:
    """Devuelve una versión aproximada en singular de una palabra en español."""
    if len(palabra) <= 3 or not palabra.endswith("s"):
        return palabra
    if palabra.endswith(("us", "is")):
        return palabra
    if palabra.endswith("es") and len(palabra) > 4 and palabra[-3] not in "aeiou":
        return palabra[:-2]
    return palabra[:-1]


def _nombre_base_archivo(tema_busqueda: str) -> str:
    """Genera un nombre de archivo seguro y en singular a partir de la búsqueda."""
    sin_acentos = (
        unicodedata.normalize("NFKD", tema_busqueda)
        .encode("ascii", "ignore")
        .decode("ascii")
        .lower()
    )
    palabras = re.findall(r"[a-z0-9]+", sin_acentos)
    base = "".join(_singularizar_palabra(palabra) for palabra in palabras)
    return base[:50] or "foto"


def _guardar_imagenes(
    urls: list[str],
    cantidad: int,
    destino: Path,
    inicio: int = 0,
    nombre_base: str = "foto",
    intentos_previos: int = 0,
) -> int:
    """Descarga y guarda las imágenes válidas. Devuelve cuántas se guardaron."""
    descargadas = 0
    intentos = intentos_previos
    urls_vistas: set[str] = set()

    for url in urls:
        if descargadas >= cantidad:
            break
        if not url or url in urls_vistas:
            continue
        urls_vistas.add(url)

        intentos += 1
        print(f"[3/4] Probando enlace {intentos}... descargando...", flush=True)

        try:
            imagen = _descargar_imagen(url)
        except Exception:
            print(
                f"[3/4] Enlace {intentos} roto o inválido, pasando al siguiente...",
                flush=True,
            )
            continue  # URL rota, bloqueada, time out, formato inválido, etc.

        if imagen is None:
            print(
                f"[3/4] Enlace {intentos} roto o lento, pasando al siguiente...",
                flush=True,
            )
            continue

        archivo = destino / f"{nombre_base}{inicio + descargadas + 1}.jpg"
        try:
            if imagen.mode != "RGB":
                imagen = imagen.convert("RGB")
            imagen.save(archivo, "JPEG", quality=90)
        except Exception:
            print(
                f"[3/4] No se pudo guardar el enlace {intentos}, "
                "pasando al siguiente...",
                flush=True,
            )
            continue

        descargadas += 1
        print(f"[4/4] Guardada: {archivo}", flush=True)

        # Ya tenemos la cantidad pedida: no abrimos ninguna conexión más.
        if descargadas >= cantidad:
            break

    return descargadas


def descargar_fotos(tema_busqueda: str, cantidad: int, carpeta: str) -> str:
    """Busca imágenes sobre cualquier tema y las descarga en una carpeta.

    Primero intenta con DuckDuckGo; si falla (por ejemplo por Ratelimit) o no
    consigue imágenes válidas, usa la API gratuita de Wikimedia Commons.

    Args:
        tema_busqueda: Tema de las imágenes a buscar (por ejemplo "girasoles",
            "autos clásicos", "atardeceres en la playa").
        cantidad: Cantidad exacta de fotos válidas que se quieren descargar.
        carpeta: Carpeta donde guardar las fotos como tema1.jpg, tema2.jpg, etc.

    Returns:
        Un mensaje corto en texto que resume el resultado de la descarga.
    """
    tema_busqueda = (tema_busqueda or "").strip()
    carpeta = (carpeta or "").strip()

    if not tema_busqueda:
        return "Error: no se indicó qué tema buscar."
    if not carpeta:
        return "Error: no se indicó la carpeta donde guardar las fotos."

    try:
        cantidad = int(cantidad)
    except (TypeError, ValueError):
        return "Error: la cantidad de fotos debe ser un número entero."

    if cantidad < 1:
        return "Error: la cantidad de fotos debe ser mayor que cero."
    if cantidad > MAX_FOTOS:
        cantidad = MAX_FOTOS

    destino = Path(carpeta).expanduser()
    destino.mkdir(parents=True, exist_ok=True)

    consulta = tema_busqueda
    nombre_base = _nombre_base_archivo(tema_busqueda)

    print(f"\n[2/4] Buscando enlaces de imágenes de '{tema_busqueda}'...", flush=True)
    urls: list[str] = []
    try:
        urls = _buscar_en_ddgs(consulta, cantidad)
    except Exception as error:
        print(
            f"[2/4] DuckDuckGo falló ({error}). Probando Wikimedia Commons...",
            flush=True,
        )

    descargadas = (
        _guardar_imagenes(urls, cantidad, destino, nombre_base=nombre_base)
        if urls
        else 0
    )

    if descargadas < cantidad:
        print("[2/4] Buscando más enlaces en Wikimedia Commons...", flush=True)
        try:
            urls_wikimedia = _buscar_en_wikimedia(consulta, cantidad)
        except Exception:
            urls_wikimedia = []
            if descargadas == 0:
                return f"Error: no se pudieron buscar imágenes de '{tema_busqueda}'."

        if urls_wikimedia:
            descargadas += _guardar_imagenes(
                urls_wikimedia,
                cantidad - descargadas,
                destino,
                inicio=descargadas,
                nombre_base=nombre_base,
                intentos_previos=descargadas,
            )

    if descargadas == 0:
        return f"Error: no se pudo guardar ninguna imagen de '{tema_busqueda}'."

    ruta = destino.resolve()
    if descargadas < cantidad:
        return (
            f"Éxito parcial: se guardaron {descargadas} de {cantidad} fotos "
            f"en la carpeta {ruta}"
        )

    return f"Éxito: se guardaron {descargadas} fotos en la carpeta {ruta}"


INSTRUCCION_SISTEMA = (
    "Sos un asistente útil que busca y descarga imágenes de cualquier temática que "
    "pida el usuario. Cuando el usuario pida imágenes, usá la herramienta "
    "descargar_fotos con el tema de búsqueda, la cantidad y la carpeta indicados. "
    "Si el usuario no indica carpeta, usá una con el nombre del tema (por ejemplo "
    "'Perros'). Si no indica cantidad, usá 4. Interpretá cada pedido de forma "
    "independiente y literal, sin mezclarlo con pedidos anteriores. Respondé siempre "
    "en español, de forma breve y clara, y confirmá cuando la descarga terminó."
)


def _crear_chat(
    cliente: genai.Client,
    modelo: str,
    historial: list[types.Content] | None = None,
) -> Chat:
    """Crea una sesión de chat con el modelo y las herramientas del agente."""
    return cliente.chats.create(
        model=modelo,
        config=types.GenerateContentConfig(
            system_instruction=INSTRUCCION_SISTEMA,
            tools=[descargar_fotos],
            # Desactivamos la ejecución automática: el bucle principal maneja
            # el tool call a mano para no hacer una segunda llamada a la API.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(
                disable=True,
            ),
        ),
        history=historial,
    )


def _es_error_503(error: Exception) -> bool:
    """Indica si el error es un 503 / UNAVAILABLE (modelo con alta demanda)."""
    if getattr(error, "code", None) == 503:
        return True

    estado = getattr(error, "status", None)
    if isinstance(estado, str) and estado.upper() == "UNAVAILABLE":
        return True

    texto = str(error).upper()
    return "503" in texto or "UNAVAILABLE" in texto


def _enviar_pedido(
    cliente: genai.Client,
    chat: Chat,
    pedido: str,
) -> types.GenerateContentResponse:
    """Envía un pedido al modelo y devuelve la respuesta cruda.

    Si el modelo principal falla por alta demanda (503 UNAVAILABLE), reintenta
    una vez después de esperar 2 segundos. Si vuelve a fallar, usa el modelo de
    respaldo para completar o confirmar la respuesta.
    """
    try:
        return chat.send_message(pedido)
    except Exception as error:
        if not _es_error_503(error):
            raise
        print(
            "[agente] El modelo principal está con alta demanda (503 UNAVAILABLE). "
            f"Reintentando en {ESPERA_REINTENTO_503} segundos..."
        )

    time.sleep(ESPERA_REINTENTO_503)

    try:
        return chat.send_message(pedido)
    except Exception as error:
        if not _es_error_503(error):
            raise
        print(
            "[agente] El modelo principal sigue sin responder. "
            f"Usando el respaldo '{MODELO_RESPALDO}'..."
        )

    chat_respaldo = _crear_chat(
        cliente,
        MODELO_RESPALDO,
        historial=chat.get_history(curated=True),
    )
    mensaje_respaldo = (
        "El modelo principal no pudo procesar el pedido por alta demanda. "
        f"Pedido original del usuario: {pedido}. "
        "Revisá el historial, completá lo que falte y confirmá el resultado."
    )
    return chat_respaldo.send_message(mensaje_respaldo)


def _llamadas_de_funcion(
    respuesta: types.GenerateContentResponse,
) -> list[types.FunctionCall]:
    """Devuelve los tool calls que pidió el modelo (lista vacía si no hubo)."""
    llamadas: list[types.FunctionCall] = []
    for candidato in respuesta.candidates or []:
        contenido = candidato.content
        if not contenido or not contenido.parts:
            continue
        for parte in contenido.parts:
            if parte.function_call:
                llamadas.append(parte.function_call)
    return llamadas


def _ejecutar_llamadas(
    llamadas: list[types.FunctionCall],
) -> list[tuple[str, str]]:
    """Ejecuta las herramientas pedidas y devuelve pares (nombre, resultado)."""
    resultados: list[tuple[str, str]] = []

    for llamada in llamadas:
        nombre = llamada.name or ""
        if nombre != descargar_fotos.__name__:
            resultados.append(
                (nombre, f"Error: herramienta desconocida '{nombre}'.")
            )
            continue

        # Nos quedamos solo con los argumentos que acepta la función.
        permitidos = inspect.signature(descargar_fotos).parameters
        argumentos = {
            clave: valor
            for clave, valor in dict(llamada.args or {}).items()
            if clave in permitidos
        }
        try:
            resultado = descargar_fotos(**argumentos)
        except Exception as error:
            resultado = f"Error: no se pudieron descargar las fotos: {error}"
        resultados.append((nombre, resultado))

    return resultados


def main() -> None:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("Error: falta la variable de entorno GEMINI_API_KEY.")
        print("Definila y volvé a ejecutar el programa, por ejemplo en PowerShell:")
        print('  $env:GEMINI_API_KEY = "tu_clave"')
        sys.exit(1)

    cliente = genai.Client(api_key=api_key)

    print("=" * 60)
    print(" Agente de fotos con Gemini + DuckDuckGo")
    print(" Ejemplo: buscá 4 fotos de perros y ponelas en Perros")
    print(" Escribí 'salir' para terminar.")
    print("=" * 60)

    while True:
        try:
            pedido = input("\nVos: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nHasta luego.")
            sys.stdout.flush()
            os._exit(0)  # evita que los hilos de ddgs cuelguen la terminal

        if not pedido:
            continue
        if pedido.lower() == "salir":
            print("Hasta luego.")
            sys.stdout.flush()
            os._exit(0)  # evita que los hilos de ddgs cuelguen la terminal

        # Cada pedido arranca con un chat nuevo: no se acumula historial de
        # conversaciones anteriores y el pedido se interpreta de forma aislada.
        chat = _crear_chat(cliente, MODELO)

        try:
            print(
                "[1/4] Consultando a Gemini para entender el pedido...",
                flush=True,
            )
            respuesta = _enviar_pedido(cliente, chat, pedido)
        except Exception as error:
            print(f"\nAgente: uy, hubo un error: {error}")
            continue

        # Ejecución manual de herramientas: no hacemos una segunda llamada a
        # Gemini para que el modelo comente el resultado.
        llamadas = _llamadas_de_funcion(respuesta)
        if llamadas:
            resultados = _ejecutar_llamadas(llamadas)
            for _, resultado in resultados:
                print(f"\n[agente] {resultado}", flush=True)
            if any(resultado.startswith("Éxito") for _, resultado in resultados):
                print("¡Listo! Se guardaron las fotos.", flush=True)
            continue

        print(f"\nAgente: {respuesta.text or '(sin respuesta)'}", flush=True)


if __name__ == "__main__":
    main()
