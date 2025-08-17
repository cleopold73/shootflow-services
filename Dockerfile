# syntax=docker/dockerfile:1

FROM public.ecr.aws/lambda/python:3.10

# Install ExifTool via cpanminus (pure-Perl)
RUN yum install -y perl perl-App-cpanminus make tar gzip && \
    cpanm --notest --no-man-pages Image::ExifTool@13.30 && \
    yum clean all && rm -rf /var/cache/yum

# Build arg pointing to the exact wheel filename produced by CI (e.g., shootflow_services-0.1.0-py3-none-any.whl)
ARG WHEEL_FILE

# Copy that exact wheel into the container
COPY dist/${WHEEL_FILE} /tmp/${WHEEL_FILE}

# Install your package
RUN pip install --no-cache-dir /tmp/${WHEEL_FILE} && rm -f /tmp/${WHEEL_FILE}

CMD ["shootflow.s3_ingest.handler"]