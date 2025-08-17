# syntax=docker/dockerfile:1

# Runtime: AWS Lambda Python base
FROM public.ecr.aws/lambda/python:3.10

# Install prerequisites and ExifTool from source (pure-Perl)
# - perl and make are needed to run the minimal install
# - curl is used to fetch the tarball
RUN yum install -y perl make curl tar gzip && \
    curl -L https://exiftool.org/exiftool-12.88.tar.gz -o /tmp/exiftool.tar.gz && \
    tar -xzf /tmp/exiftool.tar.gz -C /tmp && \
    cd /tmp/exiftool-12.88 && \
    perl Makefile.PL && make && make install && \
    cd / && rm -rf /tmp/exiftool* && \
    yum clean all && rm -rf /var/cache/yum

# Copy the prebuilt wheel from CI artifact (ensure dist/ is in build context)
COPY dist/*.whl /tmp/app.whl

# Install your package
RUN pip install --no-cache-dir /tmp/app.whl && rm -f /tmp/app.whl

# Set the Lambda handler
CMD ["shootflow.s3_ingest.handler"]