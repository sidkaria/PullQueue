import os
import re
# Use the package we installed
from slack_bolt import App, Say, BoltContext
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from variables import *
from apscheduler.schedulers.background import BackgroundScheduler

slack_token = os.environ["SLACK_BOT_TOKEN"]

# Initializes your app with your bot token and signing secret
app = App(
    token=slack_token,
    signing_secret=os.environ.get("SLACK_SIGNING_SECRET")
)

# client is needed for the reminder scheduler
client = WebClient(token=slack_token)

# which channels to send reminder to. Reset every time slack bot is reset
reminder_channels = set()

URL_REGEX = r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\(\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+'

COMPLETED_TEXT = ":white_check_mark: Completed"
ORIGINAL_MESSAGE_LINK_TEXT = "...→"

class UrlInfo(object):
  original_message: str
  original_message_permalink: str
  github_url: str
  uid: str
  repo: str
  pic_link: str
  date_submitted: str

  def __init__(self, original_message: str, github_url: str, uid: str, repo: str, original_message_permalink: str, pic_link: str, date_submitted: str):
    self.original_message = original_message.replace("\n", "\n> ")
    self.github_url = github_url.strip("><")
    self.uid = uid
    self.repo = repo
    self.original_message_permalink = original_message_permalink
    self.pic_link = pic_link
    self.date_submitted = date_submitted

@app.event({"type": "message", "subtype": None})
def on_message(body: dict, say: Say, client, context: BoltContext):
  try:
    event = body["event"]
    channel = event.get("channel")
    message = event.get("text")
    uid = event.get("user")
    ts = event.get("ts")

    message_permalink = None
    message_permalink_resp = client.chat_getPermalink(channel=channel, message_ts=ts)
    if message_permalink_resp["ok"] == True:
      message_permalink = message_permalink_resp["permalink"]

    urls = re.findall(URL_REGEX, str(message))
    if not urls:
      return

    message_until_first_link = message.partition("<http")[0].strip()

    # fetch user info
    user_profile_resp = client.users_profile_get(user=uid)
    user_image = ""
    if user_profile_resp.get("ok", False):
      user_image = user_profile_resp.get("profile")["image_48"]

    # grab all github urls in the message
    url_infos = []
    for url in urls:
      if "github.com" in url:
        repo = "unknown"
        for r in ORG_REPOS:
          if r in url:
            repo = r
            break
        url_infos.append(UrlInfo(message_until_first_link, url, uid, repo, message_permalink, user_image, ts))

    # if no github urls we return
    if not url_infos:
      return

    # find previous pinned message by this bot
    prev_message = find_prev_pinned_message(client, channel, context.bot_user_id)

    # create new list when adding prs
    new_blocks = create_new_blocks_for_add(prev_message, url_infos)

    permalink = ""

    if prev_message:
      # edit existing message
      edit_resp = client.chat_update(channel=channel, ts=prev_message["ts"], blocks=new_blocks)
      permalink = " <%s|here>" % prev_message["permalink"]
    else:
      # send new message
      message = say(channel=channel, blocks=new_blocks)
      # add new pin
      client.pins_add(channel=channel, timestamp=message.get("ts"))
      permalink_resp = client.chat_getPermalink(channel=channel, message_ts=message.get("ts"))
      if permalink_resp["ok"] == True:
        permalink = permalink_resp["permalink"]

    if SHOULD_SEND_ADD_QUEUE_MESSAGE:
      follow_up = say(text="Your PR%s been added to the queue%s." % (("s have" if len(url_infos) != 1 else " has"), permalink), thread_ts=ts)
  except SlackApiError as e:
    if e.response["error"] == "invalid_blocks":
      say(text="Slack has a hard limit of 50 blocks per message. Looks like 1. your PR request exceeded that and 2. we have too many pending PRs :triumph:\nTime to start cleaning up!")

@app.event("reaction_added")
def on_reaction_added(body: dict, say: Say, client, context: BoltContext):
  event = body["event"]
  reaction = event.get("reaction")
  uid = event.get("user")
  channel = event.get("item")["channel"]
  item_ts = event.get("item")["ts"]

  if reaction not in LIST_COMPLETE_REACTIONS:
    return

  # find text of message reacted to
  message_reacted_to = client.conversations_history(
      channel=channel,
      inclusive=True,
      oldest=item_ts,
      limit=1
  )["messages"][0]["text"]

  # find urls in message reacted to
  urls = re.findall(URL_REGEX, str(message_reacted_to))
  if not urls:
    return
  github_urls_in_message_reacted_to = []
  for url in urls:
    if "github.com" in url:
      github_urls_in_message_reacted_to.append(url.strip("><"))

  prev_message = find_prev_pinned_message(client, channel, context.bot_user_id)
  if not prev_message:
    return

  # find indices of info from original message to remove, based on which
  # index has a pr link that is in the links in the message reacted to
  found_indices = []
  for b in prev_message.get("blocks", []):
    if b.get("accessory", None):
      if b["text"]["text"].splitlines()[1].strip("><") in github_urls_in_message_reacted_to:
        found_indices.append(int(b["accessory"]["value"]))

  # run delete on the message for each of these found indices
  for x in found_indices:
    prev_message["blocks"] = create_new_blocks_for_delete(prev_message, x)
  
  edit_resp = client.chat_update(channel=channel, ts=prev_message["ts"], blocks=prev_message["blocks"])

@app.action("remove_from_queue")
def remove_from_queue(ack, payload, client, body, context: BoltContext, say: Say):
  ack()
  channel = body["channel"]["id"]

  user_id = body["user"]["id"]

  index = int(payload["value"])

  prev_message = find_prev_pinned_message(client, channel, context.bot_user_id, False)
  find_original_message_from_prev_message_and_index(prev_message, index)
  new_blocks = create_new_blocks_for_delete(prev_message, index)

  edit_resp = client.chat_update(channel=channel, ts=prev_message["ts"], blocks=new_blocks)
  if edit_resp["ok"] == True:
    github_url, original_message_ts = find_original_message_from_prev_message_and_index(prev_message, index)
    follow_up = say(text="Your PR (%s) has been completed by <@%s> and removed from the queue." % (github_url, user_id), thread_ts=original_message_ts)

@app.command("/prs")
def handle_show_prs(ack, say, client, body, context: BoltContext):
  ack()
  channel = body["channel_id"]

  # find prev message and remove its pin
  prev_message = find_prev_pinned_message(client, channel, context.bot_user_id)
  if not prev_message or not prev_message.get("blocks", None) or get_num_prs_from_message_blocks(prev_message["blocks"]) == 0:
    say("There are no pending PRs.")
    return
  client.pins_remove(channel=channel, timestamp=prev_message["ts"])

  # send new message
  message = say(text="PRs are pending.", blocks=prev_message["blocks"], channel=channel)
  # add new pin
  client.pins_add(channel=channel, timestamp=message.get("ts"))

@app.command("/start_reminders")
def add_to_reminders(ack, body):
  ack()
  channel = body["channel_id"]

  reminder_channels.add(channel)

@app.command("/stop_reminders")
def stop_reminders(ack, body, say):
  ack()
  channel = body["channel_id"]

  reminder_channels.remove(channel)
  message = say(text="You will no longer be reminded of pending PRs every morning.", channel=channel)

@app.command("/clear_completed")
def clear_completed_text(ack, client, body, context: BoltContext):
  ack()
  channel = body["channel_id"]

  prev_message = find_prev_pinned_message(client, channel, context.bot_user_id)
  if prev_message.get("blocks", None):
    client.chat_update(channel=channel, ts=prev_message["ts"], blocks=prev_message["blocks"])

@app.command("/remove_dividers")
def remove_dividers_from_message(ack, client, body, context: BoltContext):
  ack()
  channel = body["channel_id"]

  prev_message = find_prev_pinned_message(client, channel, context.bot_user_id)
  new_blocks = []
  for block in prev_message.get("blocks", []):
    if block["type"] != "divider":
      new_blocks.append(block)
  
  if prev_message.get("blocks", None):
    client.chat_update(channel=channel, ts=prev_message["ts"], blocks=new_blocks)

"""
The following methods are internal functionality for the listeners above.
"""

def find_prev_pinned_message(client, channel, bot_user_id, should_remove_completed=True):
  prev_message = None
  items = client.pins_list(channel=channel).get("items")
  if items:
    for item in items:
      if item.get("created_by") == bot_user_id:
        prev_message = item.get("message")
        # remove any completed blocks if we even show it for this bot session
        if SHOULD_SHOW_COMPLETED_TEXT and should_remove_completed:
          remove_completed_blocks_from_message(prev_message)
        break
  return prev_message

def create_new_blocks_for_add(prev_message, url_infos):
  prev_message_blocks = prev_message.get("blocks", []) if prev_message else _build_header_blocks()

  # create new blocks to add, and append them to the previous blocks
  blocks_to_add = []
  for info in url_infos:
    blocks_to_add.extend(_build_pr_blocks(info))
  new_blocks = prev_message_blocks + blocks_to_add

  # reassign values for buttons in order
  num_prs = 0
  for block in new_blocks:
    if block.get("accessory", None):
      block["accessory"]["value"] = str(num_prs)
      num_prs += 1

  if num_prs == 1:
    new_blocks[0]["text"]["text"] = "There is %s pending PR." % str(num_prs)
  else:
    new_blocks[0]["text"]["text"] = "There are %s pending PRs." % str(num_prs)
  return new_blocks

# delete a section of blocks belonging to the queue item
def create_new_blocks_for_delete(prev_message, index):  # TODO: change index param to list of indices for better performance
  prev_message_blocks = prev_message.get("blocks") if prev_message else []

  after_delete_blocks = []
  len_prev_blocks = len(prev_message_blocks)
  idx = 0
  while idx < len_prev_blocks:
    block = prev_message_blocks[idx]
    if block.get("accessory", None) and block["accessory"]["value"] == str(index):
      if SHOULD_SHOW_COMPLETED_TEXT:
        after_delete_blocks.append(_build_completed_block)
      # progress index until after the set of blocks representing the removed PR
      idx += 1
      while (idx < len_prev_blocks):
        new_block = prev_message_blocks[idx]
        if new_block.get("accessory", None) and block["accessory"]["value"]:
          break
        idx += 1
      if idx >= len_prev_blocks:
        break

    block = prev_message_blocks[idx]
    after_delete_blocks.append(block)
    idx += 1

  num_prs = get_num_prs_from_message_blocks(after_delete_blocks)
  if num_prs == 1:
    after_delete_blocks[0]["text"]["text"] = "There is %s pending PR." % str(num_prs)
  else:
    after_delete_blocks[0]["text"]["text"] = "There are %s pending PRs." % str(num_prs)

  return after_delete_blocks

def find_original_message_from_prev_message_and_index(prev_message, index):
  prev_message_blocks = prev_message.get("blocks") if prev_message else []
  for idx, block in enumerate(prev_message_blocks):
    if block.get("accessory", None) and block["accessory"]["value"] == str(index):
      text_of_section = block["text"]["text"]
      github_link = text_of_section.splitlines()[1]
      original_message_ts = prev_message_blocks[idx+1]["elements"][0]["alt_text"]
      return github_link, original_message_ts
  return None, None

# remove all blocks with ["text"]["text"] == COMPLETED_TEXT
def remove_completed_blocks_from_message(message):
  message_remove_completed_blocks = []
  for idx, b in enumerate(message["blocks"]):
    if b.get("text", None) and b["text"].get("text", None) and b["text"]["text"] == COMPLETED_TEXT:
      continue
    else:
      message_remove_completed_blocks.append(b)
  message["blocks"] = message_remove_completed_blocks

def get_num_prs_from_message_blocks(message_blocks=[]):
  return sum([block.get("accessory", None) is not None for block in message_blocks])

def _build_pr_blocks(info: UrlInfo):
  original_message_text = "> %s" % info.original_message[:100] + (" <%s|%s>" % (info.original_message_permalink, ORIGINAL_MESSAGE_LINK_TEXT) if info.original_message_permalink else "")
  return [
    {
			"type": "section",
			"text": {
				"type": "mrkdwn",
				"text": "<@%s>'s %s pull request:\n%s\n%s" % (info.uid, info.repo, info.github_url, original_message_text)
			},
			"accessory": {
				"type": "button",
				"text": {
					"type": "plain_text",
					"text": "Complete"
				},
        "style": "primary",
				"value": "",
        "action_id": "remove_from_queue"
			}
		},
    {
      "type": "context",
      "elements": [
        {
          "type": "image",
          "image_url": info.pic_link,
          "alt_text": info.date_submitted
        },
        {
          "type": "mrkdwn",
          "text": "<!date^%s^submitted {date_short_pretty} at {time}|submitted some time ago>" % str(int(float(info.date_submitted))),
        }
      ]
    },
  ]

def _build_completed_block():
  return {
    "type": "section",
    "text": {
      "type": "plain_text",
      "text": COMPLETED_TEXT
    }
  }

def _build_header_blocks():
  return [
    {
      "type": "header",
      "text": {
        "type": "plain_text",
        "text": "There are 0 pending PRs."
      }
    },
    {
      "type": "divider"
    }
  ]

# this is used for background scheduler that sends reminder message
# every morning without direct access to slack bolt client.
def send_message():
  test = client.auth_test()
  bot_id = test.get("user_id")
  if not bot_id:
    return
  for channel in reminder_channels:
    prev_message = find_prev_pinned_message(client, channel, bot_id)
    num_prs = get_num_prs_from_message_blocks(prev_message.get("blocks",[])) if prev_message else 0
    if num_prs == 1:
      message = "Good morning! There is %s pending PR." % str(num_prs)
    else:
      message = "Good morning! There are %s pending PRs." % str(num_prs)
    response = client.chat_postMessage(
      channel=channel,
      text=message
    )

# Start your app
if __name__ == "__main__":
  scheduler = BackgroundScheduler()
  scheduler.add_job(send_message, 'cron', day_of_week="mon-fri", hour=9)
  scheduler.start()
  app.start(port=int(os.environ.get("PORT", 3000)))
