FROM python:3.12-slim AS base_image

# Make port 80 available to the world outside this container
EXPOSE 80

# Set the working directory in the container
WORKDIR /code
ENV PYTHONPATH="/code"

FROM base_image AS data_historical
# Install any needed packages specified in requirements.txt
COPY ./data/historical/requirements.txt /code/
RUN pip install --no-cache-dir --upgrade -r /code/requirements.txt

# Copy the current directory contents into the container at /code
COPY ./.env /code/.env
COPY ./data/historical/app /code/data/historical/app
COPY ./routers /code/routers
COPY ./schemas /code/schemas

# Run app.py when the container launches
CMD ["uvicorn", "data.historical.app.main:app", "--host", "0.0.0.0", "--port", "80"]
