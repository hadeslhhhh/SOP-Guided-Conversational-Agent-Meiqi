FROM python:3.11-slim
WORKDIR /service
COPY sop_agent_update/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY sop_agent_update/app ./app
COPY sop_agent_update/fixtures ./fixtures
RUN useradd --create-home agent && mkdir /data && chown -R agent:agent /service /data
USER agent
ENV SESSION_DB=/data/sessions.sqlite3 PYTHONDONTWRITEBYTECODE=1
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
