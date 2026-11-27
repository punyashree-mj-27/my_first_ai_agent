import streamlit as st
import sqlite3
import os
import io
import re
import html
import json
import unicodedata
from datetime import datetime
from dotenv import load_dotenv
from groq import Groq
from pypdf import PdfReader
from gtts import gTTS
from ddgs import DDGS
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors


# ---------------- CONFIGURATION ----------------

load_dotenv()

st.set_page_config(
    page_title="My AI Copilot",
    page_icon="🤖",
    layout="wide"
)

DB_NAME = "chat_history.db"
MODEL_NAME = "openai/gpt-oss-120b"

api_key = os.getenv("GROQ_API_KEY")

if not api_key:
    st.error("GROQ_API_KEY not found. Please check your .env file.")
    st.stop()

client = Groq(api_key=api_key)


# ---------------- DATABASE ----------------

def get_connection():
    conn = sqlite3.connect(DB_NAME)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def create_database():
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS conversation_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (conversation_id)
                REFERENCES conversations(id)
        )
    """)

    cursor.execute("""
        SELECT name FROM sqlite_master
        WHERE type='table' AND name='chat_messages'
    """)

    old_table_exists = cursor.fetchone()

    cursor.execute("""
        SELECT name FROM sqlite_master
        WHERE type='table' AND name='legacy_chat_messages'
    """)

    legacy_exists = cursor.fetchone()

    if old_table_exists and not legacy_exists:

        cursor.execute("""
            SELECT id, role, content, created_at
            FROM chat_messages
            ORDER BY id
        """)

        old_messages = cursor.fetchall()

        if old_messages:
            cursor.execute("""
                INSERT INTO conversations (title)
                VALUES ('Previous Chat')
            """)

            previous_chat_id = cursor.lastrowid

            for row in old_messages:
                cursor.execute("""
                    INSERT INTO conversation_messages
                    (conversation_id, role, content, created_at)
                    VALUES (?, ?, ?, ?)
                """, (
                    previous_chat_id,
                    row[1],
                    row[2],
                    row[3]
                ))

        cursor.execute("""
            ALTER TABLE chat_messages
            RENAME TO legacy_chat_messages
        """)

    conn.commit()
    conn.close()


def create_conversation(title="New Chat"):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO conversations (title)
        VALUES (?)
    """, (title,))

    conversation_id = cursor.lastrowid

    conn.commit()
    conn.close()

    return conversation_id


def get_conversations():
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT id, title, updated_at
        FROM conversations
        ORDER BY updated_at DESC, id DESC
    """)

    conversations = cursor.fetchall()

    conn.close()

    return conversations


def get_messages(conversation_id):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT role, content
        FROM conversation_messages
        WHERE conversation_id = ?
        ORDER BY id
    """, (conversation_id,))

    messages = cursor.fetchall()

    conn.close()

    return [
        {"role": role, "content": content}
        for role, content in messages
    ]


def save_message(conversation_id, role, content):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO conversation_messages
        (conversation_id, role, content)
        VALUES (?, ?, ?)
    """, (conversation_id, role, content))

    cursor.execute("""
        UPDATE conversations
        SET updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
    """, (conversation_id,))

    conn.commit()
    conn.close()


def update_conversation_title(conversation_id, title):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        UPDATE conversations
        SET title = ?, updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
    """, (title, conversation_id))

    conn.commit()
    conn.close()


def delete_conversation(conversation_id):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        DELETE FROM conversation_messages
        WHERE conversation_id = ?
    """, (conversation_id,))

    cursor.execute("""
        DELETE FROM conversations
        WHERE id = ?
    """, (conversation_id,))

    conn.commit()
    conn.close()


def clear_conversation(conversation_id):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        DELETE FROM conversation_messages
        WHERE conversation_id = ?
    """, (conversation_id,))

    conn.commit()
    conn.close()


create_database()


# ---------------- SESSION STATE ----------------

if "current_chat_id" not in st.session_state:
    conversations = get_conversations()

    if conversations:
        st.session_state.current_chat_id = conversations[0][0]
    else:
        st.session_state.current_chat_id = create_conversation()


if "messages" not in st.session_state:
    st.session_state.messages = get_messages(
        st.session_state.current_chat_id
    )


if "pdf_text" not in st.session_state:
    st.session_state.pdf_text = ""


# ---------------- HELPER FUNCTIONS ----------------

def get_system_prompt(mode):
    prompts = {
        "General Assistant":
            "You are a helpful AI assistant. Answer clearly and accurately.",

        "Python Tutor":
            "You are a Python tutor. Explain concepts simply with examples.",

        "Interview Coach":
            "You are a software interview coach. Give interview-focused answers.",

        "PDF Assistant":
            "Answer questions using the provided PDF text when available.",

        "Code Generator":
            "Generate clean, correct, well-explained programming code.",

        "Code Debugger":
            "Help find and fix programming errors. Explain the solution.",

        "Notes Maker":
            "Create clear, organized study notes with headings and examples.",

        "Translator":
            "Translate the user's text accurately into the requested language."
    }

    return prompts.get(
        mode,
        prompts["General Assistant"]
    )


def extract_pdf_text(uploaded_files):
    all_text = []

    for uploaded_file in uploaded_files:
        try:
            reader = PdfReader(
                io.BytesIO(uploaded_file.getvalue())
            )

            for page in reader.pages:
                text = page.extract_text()

                if text:
                    all_text.append(text)

        except Exception as error:
            st.warning(
                f"Could not read {uploaded_file.name}: {error}"
            )

    return "\n\n".join(all_text)


def search_web(query):
    try:
        results = DDGS().text(
            query,
            max_results=5
        )

        search_text = []

        for result in results:
            title = result.get("title", "")
            body = result.get("body", "")
            url = result.get("href", "")

            search_text.append(
                f"Title: {title}\n"
                f"Details: {body}\n"
                f"Link: {url}"
            )

        return "\n\n".join(search_text) or "No web results found."

    except Exception as error:
        return f"Web search failed: {error}"


def ask_ai(question, mode, use_web):
    system_prompt = get_system_prompt(mode)

    if st.session_state.pdf_text:
        system_prompt += (
            "\n\nPDF content provided by the user:\n"
            + st.session_state.pdf_text[:25000]
        )

    if use_web:
        web_results = search_web(question)

        system_prompt += (
            "\n\nUse the following web search results "
            "as reference information:\n"
            + web_results
        )

    conversation_messages = [
        {
            "role": "system",
            "content": system_prompt
        }
    ]

    conversation_messages.extend(
        st.session_state.messages[-20:]
    )

    conversation_messages.append({
        "role": "user",
        "content": question
    })

    response = client.chat.completions.create(
        model=MODEL_NAME,
        messages=conversation_messages,
        temperature=0.7
    )

    return response.choices[0].message.content or ""


def generate_voice(text):
    audio_buffer = io.BytesIO()

    tts = gTTS(
        text=text,
        lang="en"
    )

    tts.write_to_fp(audio_buffer)

    audio_buffer.seek(0)

    return audio_buffer


def transcribe_audio(audio_file):
    transcription = client.audio.transcriptions.create(
        file=(
            audio_file.name,
            audio_file.getvalue()
        ),
        model="whisper-large-v3"
    )

    return transcription.text


# ---------------- PDF CLEANING ----------------

def clean_pdf_text(text):
    text = text or ""

    # Remove code block markers
    text = re.sub(
        r"```[^\n]*\n?",
        "",
        text
    )

    text = text.replace("```", "")

    # Remove Markdown headings
    text = re.sub(
        r"(?m)^\s*#{1,6}\s*",
        "",
        text
    )

    # Remove Markdown bold and italic markers
    text = text.replace("**", "")
    text = text.replace("__", "")
    text = text.replace("*", "")
    text = text.replace("`", "")

    # Remove Markdown table separator lines
    text = re.sub(
        r"(?m)^\s*\|?[\s:\-|]+\|?\s*$",
        "",
        text
    )

    # Replace table separators
    text = text.replace("|", "   ")

    # Remove horizontal lines
    text = re.sub(
        r"(?m)^\s*[-*_]{3,}\s*$",
        "",
        text
    )

    replacements = {
        "■": "",
        "▪": "-",
        "●": "-",
        "◆": "-",
        "◼": "",
        "□": "",
        "✓": "Yes",
        "✔": "Yes",
        "✗": "No",
        "➡": "->",
        "→": "->",
        "—": "-",
        "–": "-",
        "…": "...",
        "\u00a0": " ",
        "\ufeff": "",
        "\t": "    "
    }

    for old, new in replacements.items():
        text = text.replace(old, new)

    # Remove unsupported characters to prevent black boxes
    text = unicodedata.normalize(
        "NFKD",
        text
    )

    text = text.encode(
        "ascii",
        "ignore"
    ).decode("ascii")

    # Remove excessive blank lines
    text = re.sub(
        r"\n{3,}",
        "\n\n",
        text
    )

    return text.strip()


# ---------------- CREATE PDF ----------------

def create_chat_pdf(messages):
    pdf_buffer = io.BytesIO()

    document = SimpleDocTemplate(
        pdf_buffer,
        pagesize=A4,
        rightMargin=42,
        leftMargin=42,
        topMargin=42,
        bottomMargin=42
    )

    styles = getSampleStyleSheet()

    title_style = ParagraphStyle(
        "CopilotTitle",
        parent=styles["Title"],
        fontName="Helvetica-Bold",
        fontSize=17,
        leading=22,
        textColor=colors.darkblue,
        spaceAfter=16
    )

    message_style = ParagraphStyle(
        "CopilotMessage",
        parent=styles["BodyText"],
        fontName="Helvetica",
        fontSize=9.5,
        leading=14,
        spaceAfter=10,
        wordWrap="LTR"
    )

    role_style = ParagraphStyle(
        "CopilotRole",
        parent=message_style,
        fontName="Helvetica-Bold",
        textColor=colors.darkblue,
        spaceAfter=3
    )

    story = []

    story.append(
        Paragraph(
            "My AI Copilot - Chat History",
            title_style
        )
    )

    story.append(
        Paragraph(
            "Generated: "
            + datetime.now().strftime("%d-%m-%Y %H:%M"),
            styles["Normal"]
        )
    )

    story.append(Spacer(1, 16))

    for message in messages:
        role = html.escape(
            str(message.get("role", "message")).capitalize()
        )

        cleaned_text = clean_pdf_text(
            str(message.get("content", ""))
        )

        # Escape text safely for ReportLab
        safe_text = html.escape(cleaned_text)

        # Convert line breaks into PDF line breaks
        safe_text = safe_text.replace(
            "\n",
            "<br/>"
        )

        story.append(
            Paragraph(
                role + ":",
                role_style
            )
        )

        story.append(
            Paragraph(
                safe_text or "(No text)",
                message_style
            )
        )

        story.append(Spacer(1, 5))

    document.build(story)

    pdf_buffer.seek(0)

    return pdf_buffer


# ---------------- SIDEBAR ----------------

st.sidebar.title("🤖 My AI Copilot")


def create_new_chat():
    new_chat_id = create_conversation()

    st.session_state.current_chat_id = new_chat_id
    st.session_state.messages = []


st.sidebar.button(
    "➕ New Chat",
    use_container_width=True,
    on_click=create_new_chat
)


# ---------------- RENAME CHAT ----------------

st.sidebar.subheader("✏️ Rename Chat")

new_chat_title = st.sidebar.text_input(
    "Enter new chat name",
    key="rename_chat_title"
)

if st.sidebar.button("Rename This Chat"):

    if new_chat_title.strip():
        update_conversation_title(
            st.session_state.current_chat_id,
            new_chat_title.strip()
        )

        st.sidebar.success(
            "Chat renamed successfully!"
        )

        st.rerun()

    else:
        st.sidebar.warning(
            "Please enter a chat name."
        )


st.sidebar.divider()


# ---------------- SEARCH CONVERSATIONS ----------------

st.sidebar.subheader("🔍 Search Conversations")

search_text = st.sidebar.text_input(
    "Search by chat name",
    key="conversation_search"
)

st.sidebar.subheader("Your Conversations")

all_conversations = get_conversations()

visible_conversations = [
    chat for chat in all_conversations
    if not search_text.strip()
    or search_text.lower() in chat[1].lower()
]

for conversation in visible_conversations:
    chat_id = conversation[0]
    chat_title = conversation[1]

    if st.sidebar.button(
        chat_title,
        key=f"chat_{chat_id}",
        use_container_width=True
    ):
        st.session_state.current_chat_id = chat_id

        st.session_state.messages = get_messages(
            chat_id
        )

        st.rerun()


st.sidebar.divider()


# ---------------- ASSISTANT MODE ----------------

mode = st.sidebar.selectbox(
    "Assistant Mode",
    [
        "General Assistant",
        "Python Tutor",
        "Interview Coach",
        "PDF Assistant",
        "Code Generator",
        "Code Debugger",
        "Notes Maker",
        "Translator"
    ]
)


use_web = st.sidebar.checkbox(
    "🌐 Enable Web Search"
)


# ---------------- PDF UPLOAD ----------------

st.sidebar.subheader("📄 Upload PDF")

uploaded_files = st.sidebar.file_uploader(
    "Choose PDF files",
    type=["pdf"],
    accept_multiple_files=True
)

if uploaded_files and st.sidebar.button("Read PDF"):

    st.session_state.pdf_text = extract_pdf_text(
        uploaded_files
    )

    if st.session_state.pdf_text:
        st.sidebar.success(
            "PDF loaded successfully!"
        )
    else:
        st.sidebar.warning(
            "No readable text was found in the selected PDF."
        )


if st.session_state.pdf_text:
    st.sidebar.success(
        "PDF text is ready for questions."
    )

    if st.sidebar.button("Remove PDF"):
        st.session_state.pdf_text = ""
        st.rerun()


st.sidebar.divider()


# ---------------- DELETE CHAT ----------------

if st.sidebar.button("🗑️ Delete This Chat"):

    delete_conversation(
        st.session_state.current_chat_id
    )

    remaining = get_conversations()

    if remaining:
        st.session_state.current_chat_id = remaining[0][0]

        st.session_state.messages = get_messages(
            remaining[0][0]
        )

    else:
        st.session_state.current_chat_id = create_conversation()
        st.session_state.messages = []

    st.rerun()


# ---------------- CLEAR CHAT MESSAGES ----------------

if st.sidebar.button("🧹 Clear Messages in This Chat"):

    clear_conversation(
        st.session_state.current_chat_id
    )

    st.session_state.messages = []

    st.rerun()


# ---------------- MAIN APP ----------------

st.title("🤖 My AI Copilot")

st.caption(
    "Your personal AI assistant"
)


current_chat_id = st.session_state.current_chat_id

current_title = "New Chat"

for conversation in get_conversations():
    if conversation[0] == current_chat_id:
        current_title = conversation[1]
        break


st.subheader(current_title)


# ---------------- DISPLAY EXISTING MESSAGES ----------------

for message in st.session_state.messages:

    with st.chat_message(message["role"]):
        st.markdown(message["content"])


# ---------------- VOICE INPUT ----------------

st.subheader("🎤 Voice Input")

audio_file = st.audio_input(
    "Record your question"
)

if audio_file and st.button("Convert Voice to Text"):

    try:
        voice_text = transcribe_audio(
            audio_file
        )

        st.session_state.voice_question = voice_text

        st.success(
            "Voice converted successfully!"
        )

        st.text_area(
            "Recognized text",
            value=voice_text,
            key="recognized_voice"
        )

    except Exception as error:
        st.error(
            f"Voice transcription error: {error}"
        )


# ---------------- CHAT INPUT ----------------

question = st.chat_input(
    "Ask anything..."
)

if question:

    st.session_state.messages.append({
        "role": "user",
        "content": question
    })

    save_message(
        current_chat_id,
        "user",
        question
    )

    # Set title from the first question
    if len(st.session_state.messages) == 1:

        title = question[:40]

        if len(question) > 40:
            title += "..."

        update_conversation_title(
            current_chat_id,
            title
        )

    with st.chat_message("user"):
        st.markdown(question)

    with st.chat_message("assistant"):

        with st.spinner("Thinking..."):

            try:
                answer = ask_ai(
                    question,
                    mode,
                    use_web
                )

                st.markdown(answer)

                st.session_state.messages.append({
                    "role": "assistant",
                    "content": answer
                })

                save_message(
                    current_chat_id,
                    "assistant",
                    answer
                )

            except Exception as error:
                st.error(
                    f"AI error: {error}"
                )


# ---------------- DOWNLOAD CHAT AS TXT ----------------

def clean_txt_text(text):
    text = text or ""

    # Convert HTML line breaks into new lines
    text = re.sub(
        r"<br\s*/?>",
        "\n",
        text,
        flags=re.IGNORECASE
    )

    # Remove HTML tags
    text = re.sub(
        r"<[^>]+>",
        "",
        text
    )

    # Remove Markdown heading symbols
    text = re.sub(
        r"(?m)^\s*#{1,6}\s*",
        "",
        text
    )

    # Remove Markdown bold and italic symbols
    text = text.replace("**", "")
    text = text.replace("__", "")
    text = text.replace("*", "")
    text = text.replace("`", "")

    lines = text.splitlines()
    cleaned_lines = []
    table_headers = None

    for line in lines:
        line = line.strip()

        if not line:
            cleaned_lines.append("")
            continue

        # Detect Markdown table rows
        if "|" in line:
            cells = [
                cell.strip()
                for cell in line.strip("|").split("|")
            ]

            # Skip table separator rows
            if all(
                re.fullmatch(r"[:\- ]+", cell or " ")
                for cell in cells
            ):
                continue

            # First table row becomes column headings
            if table_headers is None:
                table_headers = cells
                continue

            # Convert table content into individual points
            for index, value in enumerate(cells):
                if not value:
                    continue

                if index < len(table_headers):
                    heading = table_headers[index]
                else:
                    heading = f"Point {index + 1}"

                cleaned_lines.append(
                    f"- {heading}: {value}"
                )

            cleaned_lines.append("")
            continue

        # Keep ordinary bullet points as separate lines
        line = re.sub(
            r"^[•▪●◦]\s*",
            "- ",
            line
        )

        cleaned_lines.append(line)

    result = "\n".join(cleaned_lines)

    # Remove too many blank lines
    result = re.sub(
        r"\n{3,}",
        "\n\n",
        result
    )

    return result.strip()


st.divider()

st.subheader("📥 Download Chat History")

chat_text = ""

for message in st.session_state.messages:

    role = message["role"].capitalize()

    cleaned_content = clean_txt_text(
        message["content"]
    )

    chat_text += (
        f"{role}:\n"
        f"{cleaned_content}\n\n"
    )


if chat_text:

    st.download_button(
        label="Download This Chat as TXT",
        data=chat_text,
        file_name="chat_history.txt",
        mime="text/plain"
    )



# ---------------- DOWNLOAD CHAT AS PDF ----------------

if chat_text:

    try:
        pdf_data = create_chat_pdf(
            st.session_state.messages
        )

        st.download_button(
            label="Download This Chat as PDF",
            data=pdf_data,
            file_name="chat_history.pdf",
            mime="application/pdf"
        )

    except Exception as error:
        st.error(
            f"Could not create PDF: {error}"
        )

# ---------------- CHAT EXPORT AND IMPORT ----------------

st.divider()

st.subheader("💾 Chat Backup and Restore")

# Export current conversation as JSON
if st.session_state.messages:

    export_data = {
        "chat_title": current_title,
        "messages": st.session_state.messages
    }

    json_data = json.dumps(
        export_data,
        indent=4,
        ensure_ascii=False
    )

    st.download_button(
        label="📤 Export Chat as JSON",
        data=json_data,
        file_name="my_copilot_chat.json",
        mime="application/json"
    )

# Import conversation from JSON
st.subheader("📥 Import Chat Backup")

uploaded_json = st.file_uploader(
    "Choose a chat backup JSON file",
    type=["json"],
    key="chat_import_file"
)

if uploaded_json:

    if st.button("Import Chat Backup"):

        try:
            imported_data = json.load(uploaded_json)

            imported_messages = imported_data.get(
                "messages",
                []
            )

            if not isinstance(imported_messages, list):
                st.error("Invalid chat backup format.")

            else:
                valid_messages = []

                for message in imported_messages:

                    if (
                        isinstance(message, dict)
                        and message.get("role") in ["user", "assistant"]
                        and isinstance(message.get("content"), str)
                    ):
                        valid_messages.append({
                            "role": message["role"],
                            "content": message["content"]
                        })

                if not valid_messages:
                    st.error("No valid messages found in this backup.")

                else:
                    imported_title = imported_data.get(
                        "chat_title",
                        "Imported Chat"
                    )

                    new_chat_id = create_conversation(
                        str(imported_title)[:100]
                    )

                    for message in valid_messages:
                        save_message(
                            new_chat_id,
                            message["role"],
                            message["content"]
                        )

                    st.session_state.current_chat_id = new_chat_id

                    st.session_state.messages = valid_messages

                    st.success("Chat backup imported successfully!")

                    st.rerun()

        except Exception as error:
            st.error(
                f"Import failed: {error}"
            )
            
# ---------------- SHARE CHAT ----------------

st.divider()

st.subheader("🔗 Share Chat")

if st.session_state.messages:

    share_text = f"Chat: {current_title}\n\n"

    for message in st.session_state.messages:

        role = message["role"].capitalize()
        content = message["content"]

        # Clean unwanted Markdown symbols
        content = content.replace("**", "")
        content = content.replace("##", "")
        content = content.replace("###", "")
        content = content.replace("<br>", "\n")
        content = content.replace("<br/>", "\n")
        content = content.replace("<br />", "\n")
        content = content.replace("|", " ")
        content = content.replace("```", "")
        content = content.replace("`", "")

        # Remove Markdown table separator lines
        clean_lines = []

        for line in content.splitlines():

            if line.strip() and all(
                char in "-|: " for char in line.strip()
            ):
                continue

            clean_lines.append(line)

        content = "\n".join(clean_lines)

        share_text += f"{role}:\n{content}\n\n"

    st.download_button(
        label="📤 Download Chat to Share",
        data=share_text,
        file_name="shared_chat.txt",
        mime="text/plain"
    )
# ---------------- TEXT TO SPEECH ----------------

if (
    st.session_state.messages
    and st.session_state.messages[-1]["role"] == "assistant"
):

    st.subheader(
        "🔊 Listen to Last Answer"
    )

    if st.button("Generate Voice"):

        try:
            audio = generate_voice(
                st.session_state.messages[-1]["content"]
            )

            st.audio(
                audio,
                format="audio/mp3"
            )

        except Exception as error:
            st.error(
                f"Text-to-speech error: {error}"
            )


# ---------------- FOOTER ----------------

st.divider()

st.caption(
    "My AI Copilot | Multiple Chats | PDF Assistant | "
    "Web Search | Voice | Download History"
)