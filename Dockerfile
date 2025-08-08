# Use the AWS Lambda Python base image
FROM public.ecr.aws/lambda/python:3.10

# Copy function code
COPY app.py ${LAMBDA_TASK_ROOT}

# Install dependencies (if any)
# COPY requirements.txt .
# RUN pip install --no-cache-dir -r requirements.txt

# Set the CMD to your handler (could also be a script)
CMD [ "app.lambda_handler" ]