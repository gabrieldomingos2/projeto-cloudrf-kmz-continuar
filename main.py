from fastapi import FastAPI, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
import os, zipfile, base64, httpx, re
from PIL import Image
from io import BytesIO
import numpy as np
import xml.etree.ElementTree as ET

app = FastAPI()

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Servir imagens
app.mount("/imagens", StaticFiles(directory="static/imagens"), name="imagens")

# === Função para extrair KML do KMZ ===
def extrair_kml(kmz_file: UploadFile):
    caminho_kmz = f"arquivos/{kmz_file.filename}"
    with open(caminho_kmz, "wb") as f:
        f.write(kmz_file.file.read())

    with zipfile.ZipFile(caminho_kmz, "r") as zip_ref:
        zip_ref.extractall("arquivos/kmzextraido")

    for root, _, files in os.walk("arquivos/kmzextraido"):
        for file in files:
            if file.endswith(".kml"):
                return os.path.join(root, file)
    return None

# === Parse do KML ===
def parse_kml(kml_path):
    ns = {"kml": "http://www.opengis.net/kml/2.2"}
    tree = ET.parse(kml_path)
    root = tree.getroot()
    placemarks = root.findall(".//kml:Placemark", ns)

    antena = None
    pivos = []

    for placemark in placemarks:
        nome_elem = placemark.find("kml:name", ns)
        nome = nome_elem.text if nome_elem is not None else ""

        coords_elem = placemark.find(".//kml:Point/kml:coordinates", ns)
        if coords_elem is not None:
            coords = coords_elem.text.strip().split(",")
            lon, lat = float(coords[0]), float(coords[1])

            if any(p in nome.lower() for p in ["antena", "repetidora", "torre", "barracão", "galpão", "silo"]):
                match = re.search(r"(\d+)\s?m", nome.lower())
                altura = float(match.group(1)) if match else 10.0
                antena = {
                    "nome": nome,
                    "latitude": lat,
                    "longitude": lon,
                    "altura": altura
                }
            elif "pivô" in nome.lower():
                pivos.append({
                    "nome": nome,
                    "latitude": lat,
                    "longitude": lon
                })

    return antena, pivos

# === Simular cobertura na CloudRF ===
async def simular_cloudrf(antena):
    url = "https://api.cloudrf.com/area"
    headers = {
        "key": "35113-e181126d4af70994359d767890b3a4f2604eb0ef",
        "Content-Type": "application/json"
    }

    body = {
        "version": "CloudRF-API-v3.23",
        "site": "Brazil_V6",
        "network": "My Network",
        "engine": 2,
        "coordinates": 1,
        "transmitter": {
            "lat": antena["latitude"],
            "lon": antena["longitude"],
            "alt": antena["altura"],
            "frq": 915,
            "txw": 0.3,
            "bwi": 0.1,
            "powerUnit": "W"
        },
        "receiver": {
            "lat": 0,
            "lon": 0,
            "alt": 3,
            "rxg": 3,
            "rxs": -90
        },
        "feeder": {
            "flt": 1,
            "fll": 0,
            "fcc": 0
        },
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
        "model": {
            "pm": 1,
            "pe": 2,
            "ked": 4,
            "rel": 95,
            "rcs": 1,
            "month": 5,
            "hour": 17,
            "sunspots_r12": 100
        },
        "environment": {
            "elevation": 1,
            "landcover": 1,
            "buildings": 0,
            "obstacles": 0,
            "clt": "Minimal.clt"
        },
        "output": {
            "units": "m",
            "col": "IRRICONTRO.dBm",
            "out": 2,
            "ber": 1,
            "mod": 7,
            "nf": -120,
            "res": 30,
            "rad": 10
        }
    }

    async with httpx.AsyncClient() as client:
        resp = await client.post(url, headers=headers, json=body)
        if resp.status_code != 200:
            raise Exception("Erro na API CloudRF", resp.text)
        result = resp.json()
        imagem_base64 = result["image"]
        img_data = base64.b64decode(imagem_base64)
        with open("static/imagens/sinal.png", "wb") as f:
            f.write(img_data)

        return {
            "bbox": result["latlonbox"]
        }

# === Conversão coordenadas para pixel ===
def latlon_para_pixel(lat, lon, bbox, largura, altura):
    x = int((lon - bbox["west"]) / (bbox["east"] - bbox["west"]) * largura)
    y = int((bbox["north"] - lat) / (bbox["north"] - bbox["south"]) * altura)
    return x, y

# === Verificar cobertura ===
def verificar_cobertura(pivos, bbox):
    imagem = Image.open("static/imagens/sinal.png").convert("RGB")
    largura, altura = imagem.size
    img_array = np.array(imagem)

    fora = []
    for pivo in pivos:
        x, y = latlon_para_pixel(pivo["latitude"], pivo["longitude"], bbox, largura, altura)
        if 0 <= x < largura and 0 <= y < altura:
            cor = img_array[y, x]
            if cor[0] > 200 and cor[1] > 200:  # branco/cinza
                fora.append(pivo)
        else:
            fora.append(pivo)
    return fora

# === Rota principal ===
@app.post("/processar_kmz")
async def processar_kmz(kmz: UploadFile = File(...)):
    try:
        kml_path = extrair_kml(kmz)
        antena, pivos = parse_kml(kml_path)

        if not antena:
            return JSONResponse(content={"erro": "Antena não encontrada"}, status_code=400)

        resultado = await simular_cloudrf(antena)
        bbox = resultado["bbox"]

        pivos_fora = verificar_cobertura(pivos, bbox)

        return {
            "antena": antena,
            "pivos": pivos,
            "fora_da_cobertura": pivos_fora,
            "imagem": "/imagens/sinal.png",
            "limites": bbox
        }

    except Exception as e:
        return JSONResponse(content={"erro": "Falha ao processar", "detalhe": str(e)}, status_code=500)
