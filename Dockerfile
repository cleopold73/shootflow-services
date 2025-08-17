# syntax=docker/dockerfile:1

# Accept Python version via build arg (defaults to 3.10) and use it in the base image
ARG PYTHON_VERSION=3.10
FROM public.ecr.aws/lambda/python:${PYTHON_VERSION}

# Accept ExifTool version via build arg (defaults to 13.30)
ARG EXIFTOOL_VERSION=13.30

# Install ExifTool via cpanminus (pure-Perl), pinned to the provided version
RUN yum install -y perl perl-App-cpanminus make tar gzip && \
    cpanm --notest --no-man-pages Image::ExifTool@${EXIFTOOL_VERSION} && \
    yum clean all && rm -rf /var/cache/yum

# Build arg pointing to the exact wheel filename produced by CI (e.g., shootflow_services-0.1.0-py3-none-any.whl)
ARG WHEEL_FILE

# Copy that exact wheel into the container
COPY dist/${WHEEL_FILE} /tmp/${WHEEL_FILE}

# Install your package
RUN pip install --no-cache-dir /tmp/${WHEEL_FILE} && rm -f /tmp/${WHEEL_FILE}

CMD ["shootflow.handlers.handler.handler"]