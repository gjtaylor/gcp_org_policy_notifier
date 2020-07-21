#!/usr/bin/env python3

'''
This Cloud Function compares the old available Organization Policies
to the current Organization Policies and determines if there are updates.
'''

import base64
import sys
import json
import datetime # pylint: disable=import-error
import requests # pylint: disable=import-error
import googleapiclient.discovery # pylint: disable=import-error

from os import getenv
from google.cloud import storage # pylint: disable=import-error
from google.cloud import secretmanager # pylint: disable=import-error
from google.api_core import exceptions # pylint: disable=import-error
from github import Github # pylint: disable=import-error
import tweepy # pylint: disable=import-error

def announce_kickoff(event, context):
    """
    Announces the start of the org policy comparison function.
    """
    pubsub_message = base64.b64decode(event['data']).decode('utf-8')
    print(pubsub_message)
    # Starts Logic
    compare_policies()

def compare_policies():
    '''
    Compares the old constraints vs the new ones.
    '''

    # Creates our two Org Policies lists for comparison
    old_policies = fetch_old_policies()
    current_policies = constraint_transform()

    # Sort Both Lists
    current_policies.sort()
    old_policies.sort()

    # Compare Sorted Lists
    if current_policies == old_policies:
        print("No new Org Policies Detected.")
    else:
        print("New Org Policies Detected!")
        new_policies = list(set(current_policies) - set(old_policies))

        # Create GitHub PR for new policies - save the commit to post the URL to Twitter
        github_commit = create_pr_file_content()
        # Posts new policies to slack channel - move somewhere else?
        post_to_slack(new_policies)
        # Posts to Twitter
        post_to_twitter(new_policies, github_commit)
        # Updates the GCS bucket to create our new baseline
        upload_policy_file()

def list_org_policies():
    """
    List the available Organization Policies
    """

    # Grab the Organization ID from the CFN Environment Var
    org_id = getenv('ORG_ID')

    # Create Cloud Resource Manager API Service
    service = googleapiclient.discovery.build("cloudresourcemanager", 'v1')

    # Configures the API request
    request = service.organizations().listAvailableOrgPolicyConstraints(resource=f"organizations/{org_id}")

    # Execute the API request and display any errors
    try:
        org_response = request.execute()
    except Exception as e:
        print(e)
        sys.exit(1)

    return org_response

def constraint_transform():
    """
    Transforms our List Org policy response into a list of constraint names for comparison.
    """
    #Grabs our response from the List Org Policy call
    org_response = list_org_policies()

    #Drill into constraints response
    constraints = org_response['constraints']

    # Create New Org Policies list
    # We create a list here to more easily sort and compare in compare_policies()
    current_org_policies = []
    for key in constraints:
        current_org_policies.append(key['name'])

    return current_org_policies

def fetch_old_policies():
    """
    Grabs the old Organization Policies from a GCS bucket.
    """
    # Set our GCS vars, these come from the terraform.tfvars file
    bucket_name = getenv('POLICY_BUCKET')
    source_blob_name = getenv('POLICY_FILE')

    # Create the GCS client
    storage_client = storage.Client()

    # Create our bucket variable
    bucket = storage_client.bucket(bucket_name)

    # List the objects in our GCS bucket
    files = storage_client.list_blobs(bucket)

    # Create a list of file names that we will scan for an old policy file
    file_list = []
    for gcs_file in files:
        file_list.append(gcs_file.name)

    # Check for pre-existing Org Policy File in GCS
    if source_blob_name in file_list:
        old_policies = download_policy_file()
        return old_policies
    # If file does not exist, create and upload
    else:
        upload_policy_file()

def upload_policy_file():
    """
    Uploads the new Org Policy baseline to the GCS bucket
    """
    # Grabs our new baseline in a list format
    new_policies = constraint_transform()

    # Set our GCS vars, these come from the terraform.tfvars file
    bucket_name = getenv('POLICY_BUCKET')
    source_file_name = getenv('FILE_LOCATION')
    destination_blob_name = getenv('POLICY_FILE')

    # Create the GCS client
    storage_client = storage.Client()

    # Write the new policies to our local file by converting from a list
    # to a multi-line string file
    with open(f"{source_file_name}", 'w') as policy_file:
        policy_file.write('\n'.join(new_policies))

    # Upload the new Organization Policy file to GCS
    bucket = storage_client.bucket(bucket_name)
    blob = bucket.blob(destination_blob_name)
    blob.upload_from_filename(source_file_name)

    print("New Policies Uploaded. Exiting.")
    sys.exit(0)

def download_policy_file():
    """
    Downloads the Org Policy baseline from the GCS bucket
    """
    # Set our GCS vars, these come from the terraform.tfvars file
    bucket_name = getenv('POLICY_BUCKET')
    source_blob_name = getenv('POLICY_FILE')
    destination_file_name = getenv('FILE_LOCATION')

    # Create the GCS client
    storage_client = storage.Client()

    # Create our bucket via the GCS client
    bucket = storage_client.bucket(bucket_name)

    # Creates our gcs -> prefix -> file variable
    blob = bucket.blob(source_blob_name)

    # Pulldown the baseline Org policy file
    blob.download_to_filename(destination_file_name)

    # Read contents of old policy file and turn into a list for comparison
    # We turn into a list because thats how we write the contents of list_org_policies()
    with open(f"{destination_file_name}", 'r') as policy_file:
        old_policies = [line.rstrip() for line in policy_file]
    print("Org Policy File Downloaded from GCS Bucket")

    return old_policies

def post_to_slack(new_policies):
    """
    Posts to a slack channel with the new GCP Org Policies
    """

    # Slack webhook URL
    url = get_latest_secret(getenv('S_SLACK_NAME'))

    # Get Twitter API
    tweet = create_twitter_connection()

    # Set the headers for our slack HTTP POST
    headers = {
        'Content-Type': 'application/json'
    }

    # We want to iterate through the policies and convert to JSON
    for policy in new_policies:
        # This makes the policy into a dict. Slack requires the format {"text": "data"}
        dict_policy = {"text": f"New Organization Policy Detected: {policy}"}
        # Converts to JSON for the HTTP POST payload
        payload = json.dumps(dict_policy)
        # Post to the slack channel
        try:
            requests.request("POST", url, headers=headers, data=payload)
            print("Posting to Slack")
        except Exception as e:
            print(e)
            sys.exit(1)

def create_pr_file_content():
    """
    Creates the Organization Policy file content for the GitHub Pull Request.
    """

    #Grabs our response from the List Org Policy call
    org_response = list_org_policies()

    # Create PR file content
    pr_file_content = json.dumps(org_response, indent=4)

    # Create GitHub Pull Request
    result = create_pr(pr_file_content)

    return result

def create_pr(pr_file_content):
    """
    Creates our GitHub pull request with the Organization Policy updates.
    """
    # Fetch our GitHub token from GCP Secret Manager
    github_token = get_latest_secret(getenv('S_TOKEN_NAME'))

    # Date is used in PR
    todays_date = datetime.date.today()

    # Create our GitHub authorized client
    g = Github(github_token)

    # Set our target repo
    try:
        repo = g.get_repo("ScaleSec/gcp_org_policy_notifier")
    except:
        print("There was an error reaching the repository.")
        sys.exit(1)

    # Identify which file we want to update
    repo_file_path = "policies/org_policy.json"

    # Set our branches
    default_branch = "main"
    target_branch = "new_policies"

    # Fetch our default branch
    try:
        source = repo.get_branch(f"{default_branch}")
    except:
        print("There was an error reaching the default branch.")
        sys.exit(1)
    # Create our new branch
    try:
        print("Creating a new branch.")
        repo.create_git_ref(ref=f"refs/heads/{target_branch}", sha=source.commit.sha)
    except:
        print("There was an error creating our new branch.")
        sys.exit(1)

    # Retrieve the old file to get its SHA and path
    try:
        contents = repo.get_contents(repo_file_path, ref=default_branch)
    except:
        print("There was an error fetching the old policy file.")
        sys.exit(1)

    # Update the old file with new content
    try:
        result = repo.update_file(contents.path, "New Policies Detected", pr_file_content, contents.sha, branch=target_branch)
    except:
        result = None
        print("There was an error updating the old policy file.")
        sys.exit(1)

    # Create our Pull Request
    try:
        print("Creating GitHub Pull Request.")
        repo.create_pull(title=f"New Policies Detected on {todays_date}", head=target_branch, base=default_branch, body=f"New Policies Detected on {todays_date}")
    except:
        print("There was an error creating the pull request.")
        sys.exit(1)

    return result

def get_twitter_secrets():
    """
    Retrieves Twitter credentials from Secret Manager.
    There are four secrets so this creates a dictionary with all of them by key name.
    """

    # Create a dictionary with the secret names that we will update with the values
    secret_names = {"consumer_key":f"{getenv('CONSUMER_KEY_NAME')}","consumer_key_secret":f"{getenv('CONSUMER_KEY_SECRET_NAME')}","access_token":f"{getenv('ACCESS_TOKEN_NAME')}","access_token_secret":f"{getenv('ACCESS_TOKEN_SECRET_NAME')}"}

    # Create the sercret path with the values of the secret names, get the secrets and update the dict
    secret_names = { k: get_latest_secret(v) for k,v in secret_names.items() }

    return secret_names

def create_twitter_connection():
    """
    Creates an api connection to Twitter to post content
    """
    # Retrieve a dictionary of 4 different credentials needed to authenticate with Twitter
    creds = get_twitter_secrets()

    # Auth with Twitter using Tweepy
    try:
        auth = tweepy.OAuthHandler(creds['consumer_key'], creds['consumer_key_secret'])
        auth.set_access_token(creds['access_token'], creds['access_token_secret'])
        api = tweepy.API(auth)
        return api
    except Exception as e:
        print(e)

def post_to_twitter(new_policies, commit):
    """
    Tweets with the new GCP Org Policies and the GitHub commit link.
    """

    # Get Twitter API
    tweet = create_twitter_connection()

    # We want to iterate through the policies and Tweet them out
    for policy in new_policies:
        # This makes the policy into a string with the commit URL at the end.
        content_to_post = f"New Organization Policy Detected: {policy.split('constraints/')[-1]} {commit['commit'].url}"

        # Post to Twitter
        try:
            tweet.update_status(content_to_post)
        except Exception as e:
            print(e)
            sys.exit(1)

def get_latest_secret(secret_name):
    """
    Function to get the latest secret by name.
    """

    # Set GCP Secret Manager vars
    secret_project = getenv('S_PROJECT')
    secret_version = getenv('S_VERSION', "latest")

    # Create the Secret Manager client.
    client = secretmanager.SecretManagerServiceClient()

    # Set the secret location
    secret_location = client.secret_version_path(secret_project, secret_name, secret_version)

    # Get the secret to use
    try:
        print(f"Getting {secret_name} secret.")
        response = client.access_secret_version(secret_location)
        decoded_secret = response.payload.data.decode('UTF-8').rstrip()
        return decoded_secret
    except exceptions.FailedPrecondition as e:
        print(e)

if __name__ == "__main__":
    compare_policies()