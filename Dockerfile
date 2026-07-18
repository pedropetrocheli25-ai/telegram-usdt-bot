FROM python:3.10-slim

# Instalar variables básicas y configurar la zona horaria del contenedor
ENV TZ=America/Caracas
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

WORKDIR /code

COPY ./requirements.txt /code/requirements.txt

RUN pip install --no-cache-dir --upgrade -r /code/requirements.txt

COPY . .

# Hugging Face Spaces requiere exponer el puerto 7860 obligatoriamente
ENV PORT=7860

CMD ["python", "bot.py"]
