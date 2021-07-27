import os
import re
# Use the package we installed
from slack_bolt import App, Say, BoltContext

# Initializes your app with your bot token and signing secret
app = App(
    token=os.environ.get("SLACK_BOT_TOKEN"),
    signing_secret=os.environ.get("SLACK_SIGNING_SECRET")
)

URL_REGEX = r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\(\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+'

COMPLETED_TEXT = ":white_check_mark: Completed"

# SET THIS CUSTOM TO YOUR ORGANIZATION'S REPOS
ORG_REPOS = []

NUM_HEADER_BLOCKS = 2

NUM_SECTION_BLOCKS = 3

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
    self.github_url = github_url
    self.uid = uid
    self.repo = repo
    self.original_message_permalink = original_message_permalink
    self.pic_link = pic_link
    self.date_submitted = date_submitted

@app.event({"type": "message", "subtype": None})
def reply(body: dict, say: Say, client, context: BoltContext):
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

  follow_up = say(text="Your PR%s been added to the queue%s." % (("s have" if len(url_infos) != 1 else " has"), permalink), thread_ts=ts)

@app.action("remove_from_queue")
def remove_from_queue(ack, payload, client, body, context: BoltContext):
  ack()
  channel = body["channel"]["id"]

  prev_message = find_prev_pinned_message(client, channel, context.bot_user_id, False)
  new_blocks = create_new_blocks_for_delete(prev_message, int(payload["value"]))

  edit_resp = client.chat_update(channel=channel, ts=prev_message["ts"], blocks=new_blocks)

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

def find_prev_pinned_message(client, channel, bot_user_id, should_remove_completed=True):
  prev_message = None
  items = client.pins_list(channel=channel).get("items")
  if items:
    for item in items:
      if item.get("created_by") == bot_user_id:
        prev_message = item.get("message")
        # remove any completed blocks
        if should_remove_completed:
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
def create_new_blocks_for_delete(prev_message, index):
  prev_message_blocks = prev_message.get("blocks") if prev_message else []

  after_delete_blocks = []
  idx_found = -1
  for idx, block in enumerate(prev_message_blocks):
    if idx_found == -1 and block.get("accessory", None) and block["accessory"]["value"] == str(index):
      after_delete_blocks.append(_build_completed_block())
      idx_found = idx
    elif idx_found != -1 and idx in range(idx_found+1, idx_found + NUM_SECTION_BLOCKS):
      print(block)
      continue
    else:
      after_delete_blocks.append(block)

  num_prs = get_num_prs_from_message_blocks(after_delete_blocks)
  if num_prs == 1:
    after_delete_blocks[0]["text"]["text"] = "There is %s pending PR." % str(num_prs)
  else:
    after_delete_blocks[0]["text"]["text"] = "There are %s pending PRs." % str(num_prs)

  return after_delete_blocks

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
  original_message_text = "> %s" % info.original_message[:100] + ((" <%s|...→>" % info.original_message_permalink) if info.original_message_permalink else "")
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
          "alt_text": info.uid
        },
        {
          "type": "mrkdwn",
          "text": "<!date^%s^submitted {date_short_pretty} at {time}|submitted some time ago>" % str(int(float(info.date_submitted))),
        }
      ]
    },
    {
      "type": "divider"
    }
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

# Start your app
if __name__ == "__main__":
    app.start(port=int(os.environ.get("PORT", 3000)))