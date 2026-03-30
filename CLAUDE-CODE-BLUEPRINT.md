# Sensei Second Brain — Setup Blueprint

You don't need to know how to code to run this. Just pass this blueprint to Claude Code, and it will automatically set up, install, and launch your private Second Brain for you.

---

## 🚀 Copy Everything Below This Line Into Claude Code

```text
You are an developer setup assistant. I want to build and run the Sensei "Second Brain" project located in this directory. 

I do not know how to code. Your job is to autonomously run all the terminal commands needed to install the dependencies and boot up the app so I can just use it. This app uses the native Tailwind CSS v4 Vite plugin.

Please follow these exact steps sequentially. Ask for my permission if you need to execute commands, but handle the technical details yourself.

### Step 1: Install Backend (Python)
Navigate to the `backend` folder. Check if Python is installed. If it is, run the command to install the required Python packages from the `requirements.txt` file. Make sure you use the virtual environment if necessary or just install them globally.

### Step 2: Install Frontend (Node.js)
Navigate to the `frontend` folder. Check if Node.js/npm is installed. Run `npm install` to download all the React and Tailwind v4 dependencies.

### Step 3: API Key Setup
Pause and ask me to provide my FREE Gemini API key (from Google AI Studio). 
Once I give it to you, create a `.env` file at the root of the project with:
GEMINI_API_KEY=my_key_here

### Step 4: Launch!
Launch two concurrent processes:
1. Start the FastAPI backend: `cd backend` and run `uvicorn main:app --port 8000`
2. Start the Vite React frontend: `cd frontend` and run `npm run dev`

Tell me to click the `http://localhost:5173` link in the terminal! Then tell me to drop some PDFs, Images, or Videos into the `brain_data` folder and watch the Knowledge Graph generate itself!
```
