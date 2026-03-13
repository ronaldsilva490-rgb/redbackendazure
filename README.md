# Red Backend (API)

Este é o backend do projeto **Red Comercial**, uma API REST desenvolvida em Python com Flask, integrada ao Supabase para autenticação e banco de dados.

## 🚀 Tecnologias Essenciais
* **Linguagem:** Python 3
* **Framework Web:** Flask (v3.0.3)
* **Banco de Dados e Auth:** Supabase (`supabase-py`)
* **Segurança e Autenticação:** PyJWT, bcrypt
* **Deploy e Servidor Web:** Gunicorn, Docker, Fly.io
* **Outras Ferramentas:** Reportlab (Geração de PDFs), Pillow (Processamento de Imagens)

---

## 🛠️ Configuração Inicial (Ambiente de Desenvolvimento)

### 1. Pré-requisitos
Certifique-se de ter o Python 3.8+ instalado na sua máquina.

### 2. Instalação das Dependências
1. Navegue até o diretório do backend:
   ```bash
   cd redbackend
   ```
2. Crie e ative um ambiente virtual (recomendado):
   ```bash
   python -m venv venv
   # No Windows
   venv\Scripts\activate
   # No Linux/Mac
   source venv/bin/activate
   ```
3. Instale as dependências:
   ```bash
   pip install -r requirements.txt
   ```

### 3. Variáveis de Ambiente
Copie o arquivo de exemplo para criar suas próprias configurações locais:
```bash
cp .env.example .env
```
Preencha as informações do Supabase e as chaves de segurança conforme as instruções inseridas no `.env`.

### 4. Executando o Servidor Localmente
Para iniciar a API em modo de desenvolvimento (porta 7860):
```bash
python main.py
```
A API estará acessível em `http://localhost:7860` ou `http://0.0.0.0:7860`.

---

## 🗄️ Estrutura do Banco de Dados / Supabase
O backend utiliza o Supabase não só como banco PostgreSQL, mas também aproveitando seu sistema de "Row Level Security" (RLS) e Auth nativo.
Estão inclusos vários arquivos `.sql` na raiz deste diretório:
- `COMPLETE_SCHEMA.sql` / `SALES_SCHEMA_V2.sql`: Scripts para configuração base das tabelas.
- `RLS_SETUP_INSTRUCTIONS.md`: Instruções valiosas de segurança RLS prontas para uso.
- **Importante:** Mantenha os schemas sempre alinhados entre a produção/desenvolvimento rodando os scripts pelo painel do Supabase.

---

## 🚀 Deploy
O projeto está configurado para ser feito o deploy rapidamente no **Fly.io**, utilizando a imagem construída pelo Docker.
1. Confira o guia próprio de deploy no documento: `GUIA_FLY_IO.md`.
2. O servidor de produção (WSGI) utiliza o Gunicorn, orquestrado pelos arquivos `Dockerfile` e `fly.toml`.
