from flask import Flask, request, jsonify, redirect, url_for
import requests
import json
import markdownify
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from pymongo import MongoClient
from dotenv import load_dotenv
import os, re
# Load environment variables from .env file
load_dotenv()

app = Flask(__name__)

# Slack OAuth configuration
CLIENT_ID = os.getenv('CLIENT_ID')
CLIENT_SECRET = os.getenv('CLIENT_SECRET')
REDIRECT_URI = os.getenv('REDIRECT_URI')

# MongoDB configuration
MONGO_URI = os.getenv('MONGO_URI')
client = MongoClient(MONGO_URI)
db = client['slack_chatbot']  # Use your database name

@app.route('/')
def hello():
    return "Welcome!"

@app.route('/ping')
def ping():
    try:
        # Ping the MongoDB server
        client.admin.command('ping')
        print("Pinged your MongoDB deployment. You successfully connected to MongoDB!")
        return "pinged"
    except Exception as e:
        print(f"Error: {e}")
        return "Failed to ping MongoDB", 500

@app.route('/auth/slack')
def auth_slack():
    website_id = request.args.get('website_id')
    user_id = request.args.get('user_id')  # Example of another parameter

    if not website_id or not user_id:
        return "website_id and user_id are required", 400

    state = json.dumps({'website_id': website_id, 'user_id': user_id})

    params = {
        'client_id': CLIENT_ID,
        'redirect_uri': REDIRECT_URI,
        'scope': 'channels:read, chat:write.customize, chat:write.public, chat:write, groups:read, links:read, links:write, incoming-webhook, commands',
        'state': state  # Pass the JSON-encoded state
    }
    return redirect(f"https://slack.com/oauth/v2/authorize?{'&'.join([f'{k}={v}' for k, v in params.items()])}")

@app.route('/auth/slack/callback')
def auth_slack_callback():
    code = request.args.get('code')
    state = request.args.get('state')

    if not code or not state:
        return 'Authorization failed'

    # Decode the JSON-encoded state
    try:
        state_data = json.loads(state)
        website_id = state_data.get('website_id')
        user_id = state_data.get('user_id')
    except json.JSONDecodeError:
        return 'Invalid state parameter', 400

    if not website_id or not user_id:
        return 'Authorization failed', 400

    payload = {
        'client_id': CLIENT_ID,
        'client_secret': CLIENT_SECRET,
        'code': code,
        'redirect_uri': REDIRECT_URI
    }
    response = requests.post('https://slack.com/api/oauth.v2.access', data=payload)
    if response.status_code != 200:
        return 'Authorization failed', 400

    response_data = response.json()
    access_token = response_data.get('access_token')
    workspace_id = response_data.get('team', {}).get('id')

    if not access_token or not workspace_id:
        return 'Authorization failed', 400

    db.workspaces.update_one(
        {'workspace_id': workspace_id},
        {'$set': {
            'access_token': access_token,
            'website_id': website_id,
            'user_id': user_id
        }},
        upsert=True
    )

    return 'Slack authentication successful!'


def send_message_to_slack(message, channel, team_id):
    print("team_id", team_id)
    workspace_id = team_id  # Implement logic to get workspace ID from channel
    token_data = db.workspaces.find_one({'workspace_id': workspace_id})
    if not token_data:
        print(f"Error: Access token not found for workspace {workspace_id}")
        return
    conversation_data = db.conversation.find_one({'workspace_id': workspace_id, 'channel_id': channel})

    website_id = token_data["website_id"]
    url = f"https://preprodaiapi.chatwit.ai/chat-bot/chat?website_id={website_id}&user_message={message}"
    conv_id = None
    if conversation_data:
        conv_id = conversation_data["conversation_id"]
    if conv_id:
        url = url+f"&conversation_id={conv_id}"
    res = requests.post(f"https://preprodaiapi.chatwit.ai/chat-bot/chat?website_id={website_id}&user_message={message}")
    ans = json.loads(res.content)
    message1 = ans["messages"][0]["model_output"]
    if not conv_id:
        conv_id = str(ans["id"])
        db.workspaces.update_one(
            {'workspace_id': workspace_id, 'channel_id': channel},
            {'$set': {
                'conversation_id': conv_id
            }},
            upsert=True
        )
    access_token = token_data["access_token"]
    markdown_text=markdownify.markdownify(message1, heading_style="ATX") 
    pattern = r"\[([^\]]+)\]\(([^)]+)\)"
    def replace_link(match):
        # Extract link text and URL
        link_text, url = match.groups()
        # Return the converted format
        return f"<{url}|{link_text}>"

    # Substitute the matched links with the replacement function
    message3 = re.sub(pattern, replace_link, markdown_text)
    payload = {
        'channel': channel,
        'text': message3
    }
    headers = {
        'Authorization': f'Bearer {access_token}',
        'Content-Type': 'application/json'
    }

    response = requests.post('https://slack.com/api/chat.postMessage', json=payload, headers=headers)
    if response.status_code != 200:
        print(f'Error sending message to Slack: {response.text}')
    return ''

@app.route('/slack/events', methods=['POST'])
def slack_events():
    data = json.loads(request.data)
    print("data", data)
    if 'challenge' in data:
        return data['challenge']
    else:
        team_id = data['team_id']
        event = data['event']
        if event['type'] == 'app_mention' and not 'subtype' in event and 'text' in event:
            text = event['text']
            user = event['user']
            channel = event['channel']
            response = f'You said: {text}'
            from threading import Thread
            Thread(target=send_message_to_slack, args=(text, channel, team_id)).start()

        return '', 200

if __name__ == '__main__':
    app.run(debug=True)
