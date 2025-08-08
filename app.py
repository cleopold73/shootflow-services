# Import necessary modules
import json
import logging
import boto3
import traceback

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)


def lambda_handler(event, context):
    """
    AWS Lambda handler function
    
    Parameters:
    event (dict): Input event data from the invoker
    context (LambdaContext): Runtime information provided by AWS Lambda
    
    Returns:
    dict: API Gateway compatible response
    """
    try:
        # Log the incoming event
        logger.info(f"Received event: {json.dumps(event)}")
        logger.info(f"Context: {context}")

        # Extract request parameters if needed
        # request_parameters = event.get('queryStringParameters', {})

        # Your business logic here
        message = "Lambda function executed successfully"

        # Construct successful response
        response = {
            "statusCode": 200,
            "headers": {
                "Content-Type": "application/json",
                "Access-Control-Allow-Origin": "*"  # For CORS support
            },
            "body": json.dumps({
                "message": message,
                "input": event
            })
        }

        return response

    except Exception as e:
        # Log the error
        logger.error(f"Error: {str(e)}")
        logger.error(traceback.format_exc())

        # Return error response
        return {
            "statusCode": 500,
            "headers": {
                "Content-Type": "application/json",
                "Access-Control-Allow-Origin": "*"  # For CORS support
            },
            "body": json.dumps({
                "error": str(e),
                "message": "Internal server error"
            })
        }