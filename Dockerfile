# syntax=docker/dockerfile:1

FROM public.ecr.aws/lambda/python:3.10

# Install ExifTool via cpanminus (pure-Perl)
RUN yum install -y perl perl-App-cpanminus make tar gzip && \
    cpanm --notest --no-man-pages Image::ExifTool@13.30 && \
    yum clean all && rm -rf /var/cache/yum

# Copy the prebuilt wheel from CI artifact (ensure dist/ is in build context)
COPY dist/*.whl /tmp/app.whl

RUN pip install --no-cache-dir /tmp/app.whl && rm -f /tmp/app.whl

CMD ["shootflow.s3_ingest.handler"]