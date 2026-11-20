import json
import time

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

THROTTLE_BACKOFF_SECONDS = 5


def invoke(events, context):
    client = boto3.client(
        "lambda",
        config=Config(read_timeout=events["ReadTimeout"], retries={"max_attempts": 0}),
    )
    request = json.dumps({"Handler": events["Handler"], "Input": events["Input"]}).encode()
    deadline = time.monotonic() + events["ThrottleSeconds"]
    while True:
        try:
            response = client.invoke(FunctionName=events["Function"], Payload=request)
            break
        except ClientError as error:
            throttled = error.response["Error"]["Code"] == "TooManyRequestsException"
            if not throttled or time.monotonic() >= deadline:
                raise
            time.sleep(THROTTLE_BACKOFF_SECONDS)
    result = json.loads(response["Payload"].read() or b"null")
    if response.get("FunctionError"):
        detail = result.get("errorMessage") if isinstance(result, dict) else result
        raise RuntimeError(f"{events['Handler']} failed. {detail}")
    return result
