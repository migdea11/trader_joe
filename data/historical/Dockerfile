# Use an official Python runtime as a parent image
FROM python:3.12-slim

# Use an argument in Dockerfile if needed
# ARG DATA_DIR

# Set the working directory in the container
WORKDIR /code
ENV PYTHONPATH="/code"

# Install any needed packages specified in requirements.txt
COPY ./requirements.txt /code/
RUN pip install --no-cache-dir --upgrade -r /code/requirements.txt

# Copy the current directory contents into the container at /code
COPY ./.env /code/.env
COPY ./app /code/app

# Make port 80 available to the world outside this container
EXPOSE 80

# Define environment variable
ENV MODULE_NAME="app.main"
ENV VARIABLE_NAME="app"
ENV PORT=80

# Run app.py when the container launches
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "80"]
