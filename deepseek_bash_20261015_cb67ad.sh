mkdir mock-exchange && cd mock-exchange
# put the files in place: main.py, engine.py, static/index.html, requirements.txt
python -m venv venv && source venv/bin/activate   # or venv\Scripts\activate on Windows
pip install -r requirements.txt
uvicorn main:app --reload --port 8000