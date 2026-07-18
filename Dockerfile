FROM python:3.10-slim

# Configurar la zona horaria de Venezuela para que los reportes de saldo y horas coincidan
ENV TZ=America/Caracas
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

WORKDIR /code

COPY ./requirements.txt /code/requirements.txt

RUN pip install --no-cache-dir --upgrade -r /code/requirements.txt

COPY . .

# Exponer el puerto por defecto para el servidor Flask
ENV PORT=7860

CMD ["python", "bot.py"]
