import os
from typing import Optional

import boto3


class Metrics:
    def __init__(self, namespace: Optional[str] = None, region_name: Optional[str] = None):
        self.namespace = namespace or os.getenv("METRICS_NAMESPACE", "ShootFlow")
        self.client = boto3.client("cloudwatch", region_name=region_name or os.getenv("AWS_REGION", "us-east-1"))

    def put(self, name: str, value: float = 1.0, unit: str = "Count", dimensions: Optional[list] = None) -> None:
        data = {
            "MetricName": name,
            "Value": value,
            "Unit": unit,
        }
        if dimensions:
            data["Dimensions"] = dimensions
        try:
            self.client.put_metric_data(Namespace=self.namespace, MetricData=[data])
        except Exception:
            # Best-effort; don't raise from metrics
            pass

    def count(self, name: str, value: float = 1.0) -> None:
        self.put(name=name, value=value, unit="Count")


metrics = Metrics()
