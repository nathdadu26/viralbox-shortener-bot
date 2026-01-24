FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy bot code
COPY shortener.py .

# Expose health check port
EXPOSE 8000

# Run the bot
CMD ["python", "-u", "shortener.py"]
```

---

## 3️⃣ .dockerignore:
```
__pycache__/
*.pyc
*.pyo
*.pyd
.Python
*.so
*.egg
*.egg-info/
dist/
build/
.env
.env.local
.env.*.local
.git/
.gitignore
README.md
.DS_Store
node_modules/
*.log
*.sqlite
*.db
.pytest_cache/
.coverage
htmlcov/
.vscode/
.idea/
venv/
env/
