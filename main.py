from fastapi import FastAPI, UploadFile, File, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
import os, zipfile, xml.etree.ElementTree as ET, httpx, re, shutil
from PIL import Image
import numpy as np

app = FastAPI()

# CORS liberado para Netlify
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https:\/\/.*\.netlify\.app",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Servir imagens geradas
app.mount("/imagens", StaticFiles(directory="static/imagens"), name="imagens")

def extrair_latlonbox(kml_path):
    ns = {"kml": "http://earth.google.com/kml/2.2"}
    tree = ET.parse(kml_path)
    root = tree.getroot()
    box = root.find(".//kml:LatLonBox", ns)
    if box is None:
        return None
    return {
        "north": float(box.find("kml:north", ns).text),
        "south": float(box.find("kml:south", ns).text),
        "east": float(box.find("kml:east", ns).text),
        "west": float(box.find("kml:west", ns).text),
    }

def extrair_altura_do_nome(nome):
    match = re.search(r"(\d+)\s*m", nome.lower())
    return int(match.group(1)) if match else 15

@app.post("/processar")
async def processar_kmz(request: Request, kmz: UploadFile = File(...)):
    os.makedirs("arquivos", exist_ok=True)
    os.makedirs("static/imagens", exist_ok=True)

    # Limpar diretórios antigos
    shutil.rmtree("arquivos/kmzextraido", ignore_errors=True)
    shutil.rmtree("static/imagens/kml", ignore_errors=True)

    kmz_path = f"arquivos/{kmz.filename}"
    with open(kmz_path, "wb") as f:
        f.write(await kmz.read())

    with zipfile.ZipFile(kmz_path, 'r') as zip_ref:
        zip_ref.extractall("arquivos/kmzextraido")

    kml_path = None
    for root_dir, _, files in os.walk("arquivos/kmzextraido"):
        for file in files:
            if file.endswith(".kml"):
                kml_path = os.path.join(root_dir, file)
                break
    if not kml_path:
        return JSONResponse(status_code=400, content={"erro": "KML não encontrado"})

    ns = {"kml": "http://www.opengis.net/kml/2.2"}
    tree = ET.parse(kml_path)
    root = tree.getroot()

    antena = None
    pivos = []
    circulos = []

    for placemark in root.findall(".//kml:Placemark", ns):
        nome = placemark.find("kml:name", ns)
        ponto = placemark.find(".//kml:Point/kml:coordinates", ns)
        if nome is not None and ponto is not None:
            nome_texto = nome.text.lower()
            lon, lat = map(float, ponto.text.strip().split(",")[:2])

            if any(x in nome_texto for x in ["antena", "repetidora", "torre", "barracão", "galpão", "silo"]):
                altura = extrair_altura_do_nome(nome.text)
                antena = {"nome": nome.text, "lat": lat, "lon": lon, "altura": altura}
            elif "pivô" in nome_texto:
                pivos.append({"nome": nome.text, "lat": lat, "lon": lon})

        linha = placemark.find(".//kml:LineString/kml:coordinates", ns)
        if linha is not None and nome is not None and "medida do círculo" in nome.text.lower():
            coordenadas = []
            for coord in linha.text.strip().split():
                partes = coord.split(",")
                if len(partes) >= 2:
                    try:
                        lon, lat = float(partes[0]), float(partes[1])
                        coordenadas.append([lat, lon])
                    except:
                        continue
            circulos.append({"nome": nome.text, "coordenadas": coordenadas})

    if not antena:
        return JSONResponse(status_code=400, content={"erro": "Antena principal não encontrada"})

    payload = {
        "version": "CloudRF-API-v3.23",
        "site": antena["nome"],
        "network": "My Network",
        "engine": 2,
        "coordinates": 1,
        "transmitter": {
            "lat": antena["lat"],
            "lon": antena["lon"],
            "alt": antena["altura"],
            "frq": 915,
            "txw": 0.3,
            "bwi": 0.1,
            "powerUnit": "W"
        },
        "receiver": {"lat": 0, "lon": 0, "alt": 3, "rxg": 3, "rxs": -90},
        "feeder": {"flt": 1, "fll": 0, "fcc": 0},
        "antenna": {
            "mode": "template",
            "txg": 3,
            "txl": 0,
            "ant": 1,
            "azi": 0,
            "tlt": 0,
            "hbw": 360,
            "vbw": 90,
            "fbr": 3,
            "pol": "v"
        },
        "model": {"pm": 1, "pe": 2, "ked": 4, "rel": 95, "rcs": 1, "month": 5, "hour": 17, "sunspots_r12": 100},
        "environment": {"elevation": 1, "landcover": 1, "buildings": 0, "obstacles": 0, "clt": "Minimal.clt"},
        "output": {"units": "m", "col": "IRRICONTRO.dBm", "out": 2, "ber": 1, "mod": 7, "nf": -120, "res": 30, "rad": 10}
    }

    headers = {
        "Content-Type": "application/json",
        "key": "35113-e181126d4af70994359d767890b3a4f2604eb0ef"
    }

    async with httpx.AsyncClient() as client:
        response = await client.post("https://api.cloudrf.com/area", headers=headers, json=payload)
        if response.status_code != 200:
            return JSONResponse(status_code=500, content={"erro": "Erro na API CloudRF", "detalhe": response.text})

        dados = response.json()
        imagem_url = dados["PNG_WGS84"]
        kmz_url = dados.get("kmz")

        img_resp = await client.get(imagem_url)
        with open("static/imagens/sinal.png", "wb") as f:
            f.write(img_resp.content)

        imagem = Image.open("static/imagens/sinal.png").convert("RGB")
        largura, altura = imagem.size

        if kmz_url:
            kmz_resp = await client.get(kmz_url)
            with open("static/imagens/sinal.kmz", "wb") as f:
                f.write(kmz_resp.content)
            with zipfile.ZipFile("static/imagens/sinal.kmz", "r") as zip_ref:
                zip_ref.extractall("static/imagens/kml")

        limites = None
        for root_dir, _, files in os.walk("static/imagens/kml"):
            for file in files:
                if file.endswith(".kml"):
                    limites = extrair_latlonbox(os.path.join(root_dir, file))
                    break
            if limites:
                break

    def coordenada_para_pixel(lat, lon):
        lat_n, lon_e, lat_s, lon_w = limites["north"], limites["east"], limites["south"], limites["west"]
        px = int((lon - lon_w) / (lon_e - lon_w) * largura)
        py = int((lat_n - lat) / (lat_n - lat_s) * altura)
        return max(0, min(px, largura - 1)), max(0, min(py, altura - 1))

    def esta_fora(px, py):
        entorno = 3
        for dx in range(-entorno, entorno + 1):
            for dy in range(-entorno, entorno + 1):
                nx, ny = px + dx, py + dy
                if 0 <= nx < largura and 0 <= ny < altura:
                    r, g, b = imagem.getpixel((nx, ny))
                    if g > r and g > b and g > 100:
                        return False
        return True

    for piv in pivos:
        x, y = coordenada_para_pixel(piv["lat"], piv["lon"])
        if 0 <= x < largura and 0 <= y < altura:
            imagem.putpixel((x, y), (0, 0, 255))

    imagem.save("static/imagens/sinal.png")

    pivos_fora = []
    for piv in pivos:
        x, y = coordenada_para_pixel(piv["lat"], piv["lon"])
        if esta_fora(x, y):
            pivos_fora.append(piv)

    url_base = str(request.base_url).rstrip("/")
    return {
        "imagem": f"{url_base}/imagens/sinal.png",
        "limites": limites,
        "antena": antena,
        "pivos": pivos,
        "fora_cobertura": pivos_fora,
        "circulos": circulos
    }
