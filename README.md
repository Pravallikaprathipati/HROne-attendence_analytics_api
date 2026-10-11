# Employee Attendance & Analytics API

This project implements the HROne attendance API in FastAPI with MongoDB. It includes employee creation, punch-in/punch-out flows, manual regularization with audit history, and the analytics endpoints defined in the kit.

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
# create a .env file with:
# MONGO_URI=mongodb://localhost:27017
# MONGO_DB=attendance_db
python sample_seed.py
uvicorn app.main:app --port 8000 --reload
```

## Notes

- The app expects MongoDB 6+ and creates the required indexes on startup.
- `app/main.py` contains the full API implementation and business-rule logic.
- `REVIEW.md` and `DECISIONS.md` document the defects found and the key design decisions.
