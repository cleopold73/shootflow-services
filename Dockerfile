# syntax=docker/dockerfile:1

# Runtime: AWS Lambda Python base
FROM public.ecr.aws/lambda/python:3.10

# Install exiftool (required by pyexiftool)
RUN yum install -y perl-Image-ExifTool && yum clean all && rm -rf /var/cache/yum

# Copy the prebuilt wheel from CI artifact (ensure dist/ is in build context)
COPY dist/*.whl /tmp/app.whl

# Install your package
RUN pip install --no-cache-dir /tmp/app.whl && rm -f /tmp/app.whl

# Set the Lambda handler
CMD ["shootflow.s3_ingest.handler"]