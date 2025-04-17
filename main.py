import os
import re
import requests
import time
import json
import smtplib
import base64
import datetime
from io import BytesIO
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, UploadFile, File, HTTPException, status, BackgroundTasks, Request, Form
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from pydantic_settings import BaseSettings
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from openai import OpenAI
from PIL import Image

app = FastAPI()

# CORS Configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ================= CONFIGURAÇÕES =================
class Settings(BaseSettings):
    FLEXGE_API_BASE: str = os.getenv("FLEXGE_API_BASE", "https://partner-api.flexge.com/external")
    FLEXGE_API_KEY: str = os.getenv("FLEXGE_API_KEY")
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY")
    SMTP_SERVER: str = os.getenv("SMTP_SERVER", "smtp.gmail.com")
    SMTP_PORT: int = int(os.getenv("SMTP_PORT", 587))
    SMTP_USER: str = os.getenv("SMTP_USER")
    SMTP_PASSWORD: str = os.getenv("SMTP_PASSWORD")
    ZAIA_API_KEY: str = "b8555e49-67d3-4f02-926c-37ce2effe5b3"
    ZAIA_BASE: str = "https://api.zaia.app/v1.1/api"
    ZAIA_AGENT_ID: int = 34790
    ASAAS_API_KEY: str = os.getenv("ASAAS_API_KEY")
    ASAAS_BASE: str = os.getenv("ASAAS_BASE", "https://api.asaas.com/v3")
    ALLOWED_EXTENSIONS: set = {'png', 'jpg', 'jpeg', 'gif'}
    MAX_FILE_SIZE: int = 5 * 1024 * 1024  # 5MB

    class Config:
        env_file = ".env"

settings = Settings()
print("🔑 ASAAS_API_KEY carregada com sucesso.")
client = OpenAI(api_key=settings.OPENAI_API_KEY)

# ================= MODELOS PYDANTIC =================
class EmailRequest(BaseModel):
    email: str

class EnableStudentRequest(BaseModel):
    email: str

class PaymentRequest(BaseModel):
    email: str
    valor: float
    vencimento: str

class ZaiaWebhookRequest(BaseModel):
    email: str
    chat_id: Optional[str] = None

# ========================= FLEXGE HELPERS =========================
def generate_headers():
    return {
        "x-api-key": settings.FLEXGE_API_KEY,
        "accept": "application/json",
        "Content-Type": "application/json",
        "X-Request-ID": str(time.time())
    }

def get_students(page: int = 1):
    url = f"{settings.FLEXGE_API_BASE}/students?page={page}"
    resp = requests.get(url, headers=generate_headers(), timeout=10)
    return resp.json() if resp.status_code == 200 else None

def buscar_aluno_por_email(email: str):
    page = 1
    while True:
        data = get_students(page)
        if not data or not data.get("docs"):
            return None
        for aluno in data["docs"]:
            if aluno["email"].lower() == email.lower():
                return aluno
        page += 1

def patch_student_action(student_id: str, action: str):
    url = f"{settings.FLEXGE_API_BASE}/students/{action}"
    payload = {"students": [student_id]}
    return requests.patch(url, headers=generate_headers(), json=payload, timeout=10)

def buscar_erros_gramatica(aluno_id: str):
    url = f"{settings.FLEXGE_API_BASE}/students/{aluno_id}/studied-grammars?page=1"
    resp = requests.get(url, headers=generate_headers(), timeout=10)
    return resp.json() if resp.status_code == 200 else None

# ========================= ASAAS HELPERS =========================
def asaas_headers():
    return {
        "accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "FastAPIFlexgeZaia",  # obrigatório desde 13/06/2024
        "access-token": settings.ASAAS_API_KEY  # 🔑 chave real sem Bearer!
    }

def get_or_create_customer(student: dict):
    email = student["email"].lower().strip()

    url = f"{settings.ASAAS_BASE}/customers"
    params = {"email": email}

    response = requests.get(url, headers=asaas_headers(), params=params)

    if response.status_code == 200 and response.json().get("data"):
        return response.json()["data"][0]["id"]

    payload = {
        "name": student["name"].strip(),
        "email": email,
        "mobilePhone": re.sub(r"\D", "", str(student.get("phone") or ""))[-11:] or None,
        "cpfCnpj": re.sub(r"\D", "", str(student.get("cpf") or ""))[-14:] or None
    }

    response = requests.post(url, headers=asaas_headers(), json=payload)
    response.raise_for_status()
    return response.json()["id"]

def create_payment(customer_id: str, value: float, due_date: datetime.datetime,
                   description: str = "Mensalidade"):
    payload = {
        "customer": customer_id,
        "billingType": "BOLETO",
        "value": float(value),
        "dueDate": due_date.strftime("%Y-%m-%d"),
        "description": description
    }

    try:
        resp = requests.post(
            f"{settings.ASAAS_BASE}/payments",
            headers=asaas_headers(),
            json=payload,
            timeout=10
        )

        if resp.status_code == 200:
            return resp.json()
        else:
            print(f"[ERRO] Asaas POST /payments {resp.status_code} - {resp.text}")
            raise HTTPException(
                status_code=500,
                detail=f"Erro Asaas ({resp.status_code}) ao criar pagamento"
            )

    except Exception as e:
        print(f"[EXCEÇÃO] Erro inesperado ao criar pagamento: {str(e)}")
        raise HTTPException(status_code=500, detail="Erro interno ao gerar pagamento")

def get_latest_unpaid_payment(customer_id: str):
    url = f"{settings.ASAAS_BASE}/payments"
    params = {
        "customer": customer_id,
        "status": "PENDING",
        "billingType": "BOLETO",
        "limit": 1,
        "offset": 0,
        "sort": "dueDate",
        "order": "ASC",
        "access_token": settings.ASAAS_API_KEY
    }

    try:
        resp = requests.get(url, params=params, timeout=10)

        if resp.status_code == 200:
            data = resp.json()
            if data.get("data"):
                return data["data"][0]
            else:
                print("[INFO] Nenhum boleto pendente encontrado para esse cliente.")
                return None

        else:
            print(f"[ERRO] Asaas GET /payments {resp.status_code} - {resp.text}")
            raise HTTPException(
                status_code=500,
                detail=f"Erro Asaas ({resp.status_code}) ao buscar cobrança pendente"
            )

    except Exception as e:
        print(f"[EXCEÇÃO] Erro inesperado ao buscar cobrança pendente: {str(e)}")
        raise HTTPException(status_code=500, detail="Erro interno ao consultar cobrança")

def create_checkout_flexivel(customer_id: str, valor: float):
    payload = {
        "customer": customer_id,
        "billingType": "UNDEFINED",
        "value": valor,
        "dueDate": (datetime.date.today() + datetime.timedelta(days=3)).isoformat(),
        "description": "Pagamento flexível"
    }
    r = requests.post(f"{settings.ASAAS_BASE}/payments", 
                     headers=asaas_headers(), 
                     json=payload, 
                     timeout=10)
    r.raise_for_status()
    return r.json()["invoiceUrl"]

# ========================= ZAIA HELPER =========================
def send_whatsapp_via_zaia(phone_e164: str, text: str, chat_id: Optional[str] = None):
    if not settings.ZAIA_API_KEY or not settings.ZAIA_AGENT_ID:
        return None
    payload = {
        "agentId": settings.ZAIA_AGENT_ID,
        "prompt": text,
        "streaming": False,
        "asMarkdown": True,
        "custom": {"whatsapp": phone_e164}
    }
    if chat_id:
        payload["externalGenerativeChatExternalId"] = chat_id
    headers = {
        "Authorization": f"Bearer {settings.ZAIA_API_KEY}",
        "Content-Type": "application/json"
    }
    r = requests.post(f"{settings.ZAIA_BASE}/external-generative-message/create",
                     json=payload, 
                     headers=headers, 
                     timeout=10)
    r.raise_for_status()
    return r.json()

def listar_cobrancas_assinatura(email: str):
    aluno = buscar_aluno_por_email(email)
    if not aluno:
        return {"erro": "Aluno não encontrado"}
    
    cid = get_or_create_customer(aluno)
    
    # Busca assinatura ativa
    assinatura = requests.get(
        f"{settings.ASAAS_BASE}/subscriptions",
        headers=asaas_headers(),
        params={"customer": cid, "status": "ACTIVE"},
        timeout=10
    ).json()
    
    if not assinatura.get("data"):
        return {"erro": "Nenhuma assinatura ativa"}
    
    sub_id = assinatura["data"][0]["id"]
    
    # Lista todas cobranças da assinatura
    cobrancas = requests.get(
        f"{settings.ASAAS_BASE}/subscriptions/{sub_id}/payments",
        headers=asaas_headers(),
        timeout=10
    ).json()
    
    return cobrancas

# ========================= IMAGEM + GPT‑4o =========================
def allowed_file(filename: str):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in settings.ALLOWED_EXTENSIONS

def resize_image(image_data: bytes, max_size: int = 1024):
    img = Image.open(BytesIO(image_data))
    img.thumbnail((max_size, max_size))
    buf = BytesIO()
    img_format = 'JPEG' if img.format == 'JPEG' else 'PNG'
    img.save(buf, format=img_format)
    return base64.b64encode(buf.getvalue()).decode()

def analyze_image_with_gpt4(image_base64: str):
    messages=[{
        "role":"user",
        "content":[
            {"type":"text","text":"""Analise esta imagem e responda apenas em JSON puro com a seguinte estrutura:
{
  "categoria": "1",
  "descricao": "texto explicando o que é",
  "acoes": ["ação sugerida 1", "ação sugerida 2"]
}
Categorias:
1. Comprovante de pagamento
2. Print Flexge
3. Print Notion/App
4. Outros"""},
            {"type":"image_url","image_url":{"url":f"data:image/jpeg;base64,{image_base64}","detail":"high"}}
        ]
    }]
    resp = client.chat.completions.create(model="gpt-4o", messages=messages, max_tokens=600)
    raw = resp.choices[0].message.content.strip()
    match = re.search(r'{.*}', raw, re.DOTALL)
    return json.loads(match.group(0)) if match else {"error":"JSON não encontrado"}

# ========================= EMAIL & GPT TEXT =========================
def send_inactivity_email(recipient: str, first_name: str):
    subject = "Aviso: seu acesso ao Flexge será bloqueado"
    html = f"""
    <html><body style='font-family:Montserrat;'>
    <h2 style='color:#113842;'>Hello Hello {first_name}!</h2>
    <p>Notamos que você não acessa o Flexge há alguns dias.</p>
    <p>Seu acesso será <strong>bloqueado em dois dias</strong>. Por favor, entre no app e evite isso.</p>
    <p style='margin-top:30px;'>Equipe Karol Elói Language Learning</p>
    </body></html>
    """
    msg = MIMEMultipart()
    msg['From'] = settings.SMTP_USER
    msg['To'] = recipient
    msg['Subject'] = subject
    msg.attach(MIMEText(html, 'html'))
    try:
        server = smtplib.SMTP(settings.SMTP_SERVER, settings.SMTP_PORT)
        server.starttls()
        server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
        server.send_message(msg)
        server.quit()
        return True
    except Exception as e:
        print("Erro ao enviar email:", e)
        return False

def gerar_resposta_gpt(topico: str):
    prompt = f"""
    Crie uma explicação completa sobre '{topico}' com:
    - 1 definição simples
    - 3 exemplos bilíngues (EN → PT)
    - 2 dicas práticas
    Formato: Texto simples com no máximo 5 linhas"""
    try:
        resp = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role":"system","content":"Você é um professor de inglês direto e prático, que ensina alunos com TDAH"},
                {"role":"user","content":prompt}
            ],
            timeout=10
        )
        return resp.choices[0].message.content
    except Exception:
        return f"Explicação sobre {topico} não disponível no momento. Por favor tente mais tarde."

# ========================= ROTAS FASTAPI =========================
@app.post("/analisar-imagem")
async def analisar_imagem(file: UploadFile = File(...)):
    try:
        if not any(file.filename.lower().endswith(ext) for ext in settings.ALLOWED_EXTENSIONS):
            raise HTTPException(status_code=400, detail="Formato não suportado")
        
        contents = await file.read()
        if len(contents) > settings.MAX_FILE_SIZE:
            raise HTTPException(status_code=400, detail="Arquivo excede 5MB")
        
        image_base64 = resize_image(contents)
        analysis = analyze_image_with_gpt4(image_base64)
        
        if "error" in analysis:
            raise HTTPException(status_code=500, detail=analysis["error"])
        
        if analysis["categoria"] == "2":
            return {
                "resposta": "📸 Print do Flexge detectado! Analisando desempenho...",
                "proximo_passo": "/explicacao-gramatica",
                "detalhes": analysis
            }
        
        return {"resposta": "✅ Imagem processada com sucesso!", "detalhes": analysis}
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/explicacao-gramatica")
async def explicacao_gramatica(request_data: EmailRequest):
    try:
        print("🔍 Email recebido:", request_data.email)

        aluno = buscar_aluno_por_email(request_data.email)
        print("👨‍🎓 Aluno encontrado:", aluno)

        if not aluno:
            raise HTTPException(status_code=404, detail="Aluno não encontrado")

        erros = buscar_erros_gramatica(aluno["id"])
        print("📚 Erros de gramática:", erros)

        if not erros:
            return {"resposta": "🌟 Nenhum erro recente!", "status": "sucesso"}

        resposta = "📊 *Análise Flexge* 📊\n\n"
        for t in sorted(erros, key=lambda x: x["errorPercentage"], reverse=True)[:3]:
            explic = gerar_resposta_gpt(t["name"])
            resposta += f"📌 **{t['name']} ({t['errorPercentage']}%)**\n{explic}\n-------------------------\n"

        return {"resposta": resposta.strip(), "status": "sucesso"}

    except Exception as e:
        print("❌ ERRO:", e)
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/habilitar-aluno")
async def habilitar_aluno(request_data: EnableStudentRequest):
    try:
        print(f"[DEBUG] E-mail recebido: {request_data.email}")
        
        aluno = buscar_aluno_por_email(request_data.email)
        if not aluno:
            print("[ERRO] Aluno não encontrado:", request_data.email)
            raise HTTPException(status_code=404, detail="Aluno não encontrado")
        
        print(f"[AÇÃO] Habilitando aluno: {aluno['id']}")
        resp = patch_student_action(aluno["id"], "enable")
        
        if resp.status_code == 200:
            return {"status": "Aluno habilitado com sucesso"}
        
        print("[ERRO Flexge]", resp.text)
        raise HTTPException(status_code=400, detail=resp.text)
    
    except Exception as e:
        print(f"[ERRO GERAL] {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/check-inatividade")
async def check_inatividade(background_tasks: BackgroundTasks):
    try:
        page, bloqueados, avisados = 1, 0, 0
        hoje = datetime.datetime.utcnow()
        
        while True:
            dados = get_students(page)
            if not dados or not dados.get("docs"):
                break
            
            for aluno in dados["docs"]:
                last = aluno.get("lastAccess")
                if not last:
                    continue
                
                dias = (hoje - datetime.datetime.fromisoformat(last.replace("Z", "+00:00"))).days
                if dias >= 10:
                    patch_student_action(aluno["id"], "disable")
                    bloqueados += 1
                elif dias >= 8:
                    background_tasks.add_task(
                        send_inactivity_email,
                        aluno["email"],
                        aluno["name"].split()[0]
                    )
                    avisados += 1
            
            page += 1
        
        return {"bloqueados": bloqueados, "avisados": avisados}
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/enviar-boleto")
async def enviar_boleto(request_data: PaymentRequest):
    aluno = buscar_aluno_por_email(request_data.email)
    if not aluno:
        raise HTTPException(status_code=404, detail="Aluno não encontrado")
    
    try:
        cid = get_or_create_customer(aluno)
        pay = create_payment(cid, request_data.valor, datetime.datetime.fromisoformat(request_data.vencimento))
        link = pay.get("bankSlipUrl") or pay.get("invoiceUrl")
        
        whatsapp = re.sub(r"\D", "", aluno.get("phone", ""))
        if whatsapp:
            msg = f"Olá {aluno['name'].split()[0]}! 👋\n\nAqui está o seu boleto: {link}\n\nQualquer dúvida estou à disposição."
            send_whatsapp_via_zaia(whatsapp, msg)
        
        return {"status": "enviado", "link": link}
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/zaia/reenviar-boleto")
async def zaia_reenviar_boleto(payload: EmailRequest):
    try:
        email = payload.email
        if not email:
            return {"erro": "Email é obrigatório"}

        # Criando aluno fictício apenas com email
        aluno = {"email": email, "name": email}
        print("🔍 Buscando ou criando cliente no Asaas...")
        cid = get_or_create_customer(aluno)
        print(f"👤 Cliente encontrado: {cid}")

        # 2. Buscar assinatura ativa
        response = requests.get(
            f"{settings.ASAAS_BASE}/subscriptions",
            headers=asaas_headers(),
            params={"customer": cid}
        )
        response.raise_for_status()
        assinaturas = response.json().get("data", [])

        assinatura_ativa = next((s for s in assinaturas if s["status"] == "ACTIVE"), None)
        if not assinatura_ativa:
            return {"erro": "Nenhuma assinatura ativa encontrada"}

        subscription_id = assinatura_ativa["id"]
        print(f"📦 Assinatura ativa encontrada: {subscription_id}")

        # 3. Buscar cobrança pendente vinculada à assinatura
        response = requests.get(
            f"{settings.ASAAS_BASE}/payments",
            headers=asaas_headers(),
            params={"subscription": subscription_id, "status": "PENDING"}
        )
        response.raise_for_status()
        cobrancas = response.json().get("data", [])

        if not cobrancas:
            return {"erro": "Nenhuma cobrança pendente encontrada"}

        cobranca = cobrancas[0]
        print(f"💸 Cobrança pendente encontrada: {cobranca['id']}")

        return {
            "nome": aluno["name"],
            "vencimento": cobranca["dueDate"],
            "valor": cobranca["value"],
            "boleto_url": cobranca.get("bankSlipUrl") or cobranca.get("invoiceUrl")
        }

    except requests.exceptions.HTTPError as e:
        print(f"[ERRO HTTP] {e}")
        return {"erro": f"Erro HTTP: {str(e)}"}
    except Exception as e:
        print(f"[ERRO GERAL] {e}")
        return {"erro": f"Erro inesperado: {str(e)}"}

try:
    r = requests.post(
        f"{settings.ASAAS_BASE}/subscriptions/{sub_id}/changeBillingType",
        params={"access_token": settings.ASAAS_API_KEY},
        json={"billingType": "CREDIT_CARD"},
        timeout=10
    )
    r.raise_for_status()
except requests.exceptions.HTTPError as e:
    if r.status_code == 404:
        raise HTTPException(
            status_code=404,
            detail="Assinatura não encontrada no Asaas. Verifique se ela foi criada corretamente."
        )
    raise HTTPException(status_code=500, detail=str(e))

@app.post("/trocar-assinatura-cartao")
async def trocar_assinatura_cartao(request_data: EmailRequest):
    try:
        aluno = buscar_aluno_por_email(request_data.email)
        if not aluno:
            raise HTTPException(status_code=404, detail="Aluno não encontrado")

        cid = get_or_create_customer(aluno)

        # Buscar assinatura ativa
        assinatura_r = requests.get(
            f"{settings.ASAAS_BASE}/subscriptions",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "FastAPIFlexgeZaia",
                "access_token": settings.ASAAS_API_KEY
            },
            params={
                "customer": cid,
                "status": "ACTIVE",
                "limit": 1
            },
            timeout=10
        )

        assinatura_r.raise_for_status()
        assinatura_data = assinatura_r.json()

        if not assinatura_data.get("data"):
            raise HTTPException(status_code=404, detail="Assinatura ativa não encontrada")

        sub_id = assinatura_data["data"][0]["id"]

        # Atualizar assinatura com novo billingType (cartão) e atualizar boletos pendentes
        put_r = requests.put(
            f"{settings.ASAAS_BASE}/subscriptions/{sub_id}",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "FastAPIFlexgeZaia",
                "access_token": settings.ASAAS_API_KEY
            },
            json={
                "billingType": "CREDIT_CARD",
                "updatePendingPayments": True,
                "status": "ACTIVE",
                "nextDueDate": datetime.date.today().isoformat()
            },
            timeout=10
        )

        put_r.raise_for_status()
        response_data = put_r.json()

        return {
            "mensagem": "Assinatura atualizada para cartão com sucesso.",
            "assinatura_id": sub_id,
            "proxima_cobranca": response_data.get("nextDueDate"),
            "tipo_pagamento": response_data.get("billingType")
        }

    except requests.exceptions.HTTPError as e:
        print(f"[ERRO HTTP] {e}")
        raise HTTPException(status_code=500, detail=f"Erro HTTP: {str(e)}")
    except Exception as e:
        print(f"[ERRO GERAL] {e}")
        raise HTTPException(status_code=500, detail=f"Erro inesperado: {str(e)}")
    
@app.post("/trocar-assinatura-boleto")
async def trocar_assinatura_boleto(request_data: EmailRequest):
    try:
        aluno = buscar_aluno_por_email(request_data.email)
        if not aluno:
            raise HTTPException(status_code=404, detail="Aluno não encontrado")

        cid = get_or_create_customer(aluno)

        # Buscar assinatura ativa
        assinatura_r = requests.get(
            f"{settings.ASAAS_BASE}/subscriptions",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "FastAPIFlexgeZaia",
                "access_token": settings.ASAAS_API_KEY
            },
            params={
                "customer": cid,
                "status": "ACTIVE",
                "limit": 1
            },
            timeout=10
        )

        assinatura_r.raise_for_status()
        assinatura_data = assinatura_r.json()

        if not assinatura_data.get("data"):
            raise HTTPException(status_code=404, detail="Assinatura ativa não encontrada")

        sub_id = assinatura_data["data"][0]["id"]

        # Atualizar para BOLETO
        put_r = requests.put(
            f"{settings.ASAAS_BASE}/subscriptions/{sub_id}",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "FastAPIFlexgeZaia",
                "access_token": settings.ASAAS_API_KEY
            },
            json={
                "billingType": "BOLETO",
                "updatePendingPayments": True,
                "status": "ACTIVE",
                "nextDueDate": datetime.date.today().isoformat()
            },
            timeout=10
        )

        put_r.raise_for_status()
        response_data = put_r.json()

        return {
            "mensagem": "Assinatura atualizada para boleto com sucesso.",
            "assinatura_id": sub_id,
            "proxima_cobranca": response_data.get("nextDueDate"),
            "tipo_pagamento": response_data.get("billingType")
        }

    except requests.exceptions.HTTPError as e:
        print(f"[ERRO HTTP] {e}")
        raise HTTPException(status_code=500, detail=f"Erro HTTP: {str(e)}")
    except Exception as e:
        print(f"[ERRO GERAL] {e}")
        raise HTTPException(status_code=500, detail=f"Erro inesperado: {str(e)}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))