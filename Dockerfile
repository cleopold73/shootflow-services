# syntax=docker/dockerfile:1

# Accept Python version via build arg (defaults to 3.10) and use it in the base image
ARG PYTHON_VERSION=3.10
FROM public.ecr.aws/lambda/python:${PYTHON_VERSION}

# Accept ExifTool version via build arg (defaults to 13.30)
ARG EXIFTOOL_VERSION=13.30

# Install the verified pure-Perl release directly. Amazon Linux's cpanminus
# dependency chain conflicts with the Lambda base image's openssl-snapsafe-libs.
# Update this checksum together with EXIFTOOL_VERSION.
ADD --checksum=sha256:885afd06c4efcc60d1df703cc88ba7ddc3bb6fed854cfbaa9e6cd72adfbe8da9 \
    https://cpan.metacpan.org/authors/id/E/EX/EXIFTOOL/Image-ExifTool-${EXIFTOOL_VERSION}.tar.gz /tmp/exiftool.tar.gz
RUN yum install -y perl perl-ExtUtils-MakeMaker make tar gzip && \
    tar -xzf /tmp/exiftool.tar.gz -C /tmp && \
    cd /tmp/Image-ExifTool-${EXIFTOOL_VERSION} && \
    perl Makefile.PL && make install && \
    test "$(exiftool -ver)" = "${EXIFTOOL_VERSION}" && \
    cd / && rm -rf /tmp/Image-ExifTool-* /tmp/exiftool.tar.gz && \
    yum clean all && rm -rf /var/cache/yum

# Build arg pointing to the exact wheel filename produced by CI (e.g., shootflow_services-0.1.0-py3-none-any.whl)
ARG WHEEL_FILE

# Copy that exact wheel into the container
COPY dist/${WHEEL_FILE} /tmp/${WHEEL_FILE}

# Install your package
RUN pip install --no-cache-dir /tmp/${WHEEL_FILE} && rm -f /tmp/${WHEEL_FILE}

CMD ["shootflow.handlers.handler.handler"]
