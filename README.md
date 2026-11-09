flowpilot-ai/
├── backend/            FastAPI + Motor (MongoDB) + OpenAI + PayPal
│   ├── app/{main,config,db,paypal,agent,routes}.py
│   ├── requirements.txt
│   └── .env.example
├── frontend/           React (Vite)
│   ├── src/{main,App,api}.jsx|js, styles.css
│   ├── vercel.json
│   └── .env.example
└── render.yaml         Render blueprint for the API
# 1. Backend
cd backend
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                    # fill in PayPal / OpenAI / Mongo values
uvicorn app.main:app --reload --port 8000

# 2. Frontend (new terminal)
cd frontend
npm install
cp .env.example .env
npm run dev                                             # http://localhost:5173
curl http://localhost:8000/api/paypal/health            # {"ok":true,...} = credentials work
curl -X POST http://localhost:8000/api/seed             # creates 5 invoices in your sandbox
