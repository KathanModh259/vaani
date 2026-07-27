"""
Vaani - AI Phone Receptionist System (Multilingual Edition)

Production-grade multilingual voice agent supporting Hindi, Gujarati, English,
and natural code-switched speech combinations for small businesses in India.

## Key Differentiators
- Native Indian language support (Hindi, Gujarati, English)
- Code-switching handling within single utterances
- STT/TTS provider benchmarking across Indian languages
- Comprehensive multilingual evaluation harness
- Multi-tenant architecture for business isolation

## System Architecture
```mermaid
graph LR
    Start[Customer Calls] --> Twilio[WebSocket Media Stream]
    Twilio --> Transcribe[Multilingual STT (Bhashini/AI4Bharat/Whisper/Deepgram)]
    Transcribe --> DetectLanguage[Language Detection]
    DetectLanguage --> LLM[Multilingual LLM Agent with Context]
    LLM --> Actions[Function Calls]
    Actions --> TTS[Multilingual TTS (Bhashini/AI4Bharat/ElevenLabs)]
    TTS --> Twilio
    Twilio --> Dashboard[Business Dashboard]
    Twilio --> DB[PostgreSQL Database]

    subgraph "Database Tables"
        DB --> Appointments[appointments]
        DB --> CallLogs[call_logs]
        DB --> Services[services]
        DB --> Businesses[businesses]
    end

    subgraph "Backend"
        DB --> Python[Python/FastAPI]
        Python --> Routes[API Routes]
        Python --> WebSocket[Real-time]
    end

    subgraph "Frontend"
        Dashboard --> React[React/Tailwind]
        React --> Multi[Multi-tenant]
    end
```

## Key Features

### 1. Multilingual STT (Speech-to-Text)
- Bhashini (Government of India API) - Primary
- AI4Bharat IndicWhisper - Open-source
- Whisper - Baseline
- Deepgram - Streaming baseline
- Provider benchmarking with Hindi/Gujarati/English/code-switch accuracy

### 2. Multilingual LLM Agent
- Code-switching understanding and response
- Language detection and adaptation utterance by utterance
- Function calling for booking, FAQs, human escalation
- Context-aware conversation management

### 3. Multilingual TTS (Text-to-Speech)
- Bhashini (Government of India API) - Primary for Hindi/Gujarati
- AI4Bharat TTS - Natural Indian speech
- ElevenLabs - High-quality comparison
- Language-specific voice selection
- Interruption support

### 4. Language Management
- Automatic language detection from audio/text
- Code-switching support (Hindi-English, Gujarati-English, all-three)
- Language monitoring throughout conversations
- Consistent response language matching caller

### 5. Business Dashboard
- Multilingual business management
- Language distribution analytics
- Call analytics with language tracking
- Multi-tenant isolation

### 6. Comprehensive Evaluation
- 25+ multilingual test calls
- Language-specific benchmarking
- Code-switching accuracy measurement
- Performance metrics across languages

## Installation

### Prerequisites
- Python 3.9+
- PostgreSQL database
- Docker (recommended)

### Backend Setup

1. Clone the repository:
```bash
git clone https://github.com/Kathan/vaani-ai-phone-receptionist.git
cd vaani-ai-phone-receptionist
```

2. Create environment file:
```bash
cp .env.example .env
```

3. Install Python dependencies:
```bash
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

4. Initialize database:
```bash
# Using Docker
Docker run -d -p 5432:5432 --name vaani_db postgres
docker compose up -d

# Or using PostgreSQL directly
python -c "from db.db_models.base import Base, engine; Base.metadata.create_all(bind=engine())"
```

5. Start the backend:
```bash
cd backend/voice_agent
python __main__.py
```

### Frontend Setup (Optional)

```bash
cd frontend/ai-assistant
npm install
npm run dev
```

Open `http://localhost:3000/dashboard`. The dashboard reads live data from the
backend at `http://localhost:8000`; set `BACKEND_API_URL` in the frontend
environment when deploying it elsewhere.

## Running Tests

### Evaluation Harness
```bash
cd backend/voice_agent
python -m evaluation.run_evaluations --config evaluation/test_suite_multilingual.yaml
```

### Benchmark Language Performance
```bash
cd backend/voice_agent
eval run-benchmarks --config evaluation/language_benchmark.yaml
```

### Specific Language Tests
```bash
pytest tests/test_hindi.py -v
pytest tests/test_gujarati.py -v  
pytest tests/test_english.py -v
pytest tests/test_codeswitch.py -v
```

### Booking and Concurrency Tests
```bash
pytest tests/test_concurrent_booking.py -v
```

## Docker Compose

### Development Setup
```yaml
version: '3.8'
services:
  backend:
    build: ./backend/voice_agent
    ports:
      - "8000:8000"
    environment:
      - TWILIO_ACCOUNT_SID=${TWILIO_ACCOUNT_SID}
      - TWILIO_AUTH_TOKEN=${TWILIO_AUTH_TOKEN}
      - TWILIO_PHONE_NUMBER=${TWILIO_PHONE_NUMBER}
      # STT API Keys
      - BHASHINI_API_KEY=${BHASHINI_API_KEY}
      - AI4BHARAT_API_KEY=${AI4BHARAT_API_KEY}
      - WHISPER_API_KEY=${WHISPER_API_KEY}
      - DEEPGRAM_API_KEY=${DEEPGRAM_API_KEY}
      # TTS API Keys
      - ELEVENLABS_API_KEY=${ELEVENLABS_API_KEY}
      # LLM API Keys
      - OPENAI_API_KEY=${OPENAI_API_KEY}
      - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
      - CLAUDE_API_KEY=${CLAUDE_API_KEY}
      # Database
      - DATABASE_URL=postgresql://user:password@db/vaani
    depends_on:
      - db
    restart: unless-stopped

  frontend:
    build: ./frontend/dashboard
    ports:
      - "3000:3000"
    environment:
      - REACT_APP_API_URL=http://backend:8000
    depends_on:
      - backend
    restart: unless-stopped

  db:
    image: postgres:14
    environment:
      - POSTGRES_DB=vaani
      - POSTGRES_USER=user
      - POSTGRES_PASSWORD=password
      - POSTGRES_HOST_AUTH_METHOD=trust
    volumes:
      - postgres_data:/var/lib/postgresql/data
    restart: unless-stopped

  evaluation:
    build: ./backend/voice_agent
    command: python -m evaluation.run_evaluations --config evaluation/test_suite_multilingual.yaml
    environment:
      - DATABASE_URL=postgresql://user:password@db/vaani
    depends_on:
      - backend
      - db
    restart: unless-stopped

volumes:
  postgres_data:
```

## Usage

### API Endpoints

**Voice Calls:**
- `POST /api/voice/call` - Start a new voice call with multilingual support
- `WEBSOCKET /api/voice/ws` - WebSocket for real-time audio streaming
- `GET /api/voice/status/{call_id}` - Get call status with language info

**Business Management:**
- `GET /api/dashboard/businesses` - List all businesses
- `POST /api/dashboard/businesses` - Create new business with default languages
- `PUT /api/dashboard/businesses/{id}` - Update business settings

**Appointments:**
- `POST /api/dashboard/appointments` - Schedule appointment (supports Hindi/Gujarati/English)
- `GET /api/dashboard/appointments?business_id={id}&date={date}` - List appointments
- `PUT /api/dashboard/appointments/{id}` - Update appointment

**Call Analytics:**
- `GET /api/dashboard/analytics/calls?business_id={id}&start={date}&end={date}` - Language-aware call metrics
- `GET /api/dashboard/analytics/languages?business_id={id}` - Language usage distribution
- `GET /api/dashboard/analytics/performance?business_id={id}&language={lang}` - Performance by language

### Sample Multilingual Call Flow

1. Customer calls business phone number
2. Twilio detects caller's language (H/F/E or mix) from initial audio
3. Twilio Media Stream forwards call to `/api/voice/ws`
4. Multilingual STT transcribes audio (Hindi/Gujarati/English with code-switch support)
5. Language detection determines mix and predominant language
6. LLM agent extracts intent and takes language-appropriate action:
   - `BOOK_APPOINTMENT`: Calls database with SELECT FOR UPDATE (prevents double-booking)
   - `ANSWER_FAQ`: Returns pre-defined FAQ in caller's language(s)
   - `ESCALATE_TO_HUMAN`: Routes to human agent with language context
7. Agent response is converted to speech using matched language(s)
8. Audio response streams back to caller in <500ms

### Business Dashboard Features

**Business Settings:**
- Configure supported languages (Hindi, Gujarati, English)
- Manage service catalog with multilingual descriptions
- Set up holiday schedules per language
- Configure business contact information

**Appointment Management:**
- View upcoming appointments with language indicators
- Schedule new appointments (AI-assisted in any language)
- Cancel or reschedule with multi-language support
- Generate appointment reports by language

**Call Analytics:**
- Call volume and duration metrics by language
- Conversion rates (booking vs. escalation) across languages
- FAQ accuracy rates for each language
- Performance benchmarks (pure vs. code-switched)

## Testing

### Multilingual Evaluation
The evaluation system tests 25+ real-world scenarios across all language combinations:

**Language-Specific Tests:**
- Pure Hindi scenarios
- Pure Gujarati scenarios
- Pure English scenarios
- Hindi-English code-switch
- Gujarati-English code-switch
- All-three (H+G+E) code-switch

**Interaction Scenarios:**
- Background noise handling
- Caller interrupting mid-sentence
- Caller changing mind mid-call
- Out-of-scope questions
- Long silences or dropped calls
- Code-switch edge cases

**Performance Metrics:**
- **Booking Accuracy**: % of bookings successfully confirmed across all languages
- **Latency**: Time from user speaking to AI response (target <500ms)
- **Hallucination Rate**: % of incorrect confirmations (language-dependent)
- **Escalation Correctness**: % of calls properly routed to humans with language context
- **FAQ Completeness**: % of questions answered without escalation (by language)
- **Code-Switch Accuracy**: % of mixed-language utterances correctly understood

Sample multilingual evaluation results:
```
Call ID          | Hindi | Gujarati | English | Code-Switch | Overall Score
-----------------------------------------------------------------
2026-07-20-001   |   88% |      92% |      95% |         76% |       88%
2026-07-20-002   |   92% |      85% |      88% |         68% |       82%
2026-07-20-003   |   95% |      90% |      91% |         82% |       90%
-----------------------------------------------------------------
Average         |   92% |      89% |      91% |         75% |        87%
```

## Key Multilingual Design Decisions

### 1. Language Provider Selection
- **Bhashini (Primary)**: Government API, best quality for Hindi/Gujarati
- **AI4Bharat (Secondary)**: Open-source, good code-switching capability
- **Whisper (Baseline)**: Industry standard for English
- **Deepgram (Streaming)**: Low-latency audio for streaming use case

### 2. Code-Switching Handling
- **Auto-detection**: No language pre-selection required
- **Utterance-by-utterance**: Language may change mid-conversation
- **Mixed responses**: System can reply in multiple languages
- **Context tracking**: Maintain conversational context across language switches

### 3. Language-Specific Features
- **Bhashini TTS**: Native Indian speech synthesis
- **Hindi/Gujarati support**: Better than ElevenLabs for Indian languages
- **ElevenLabs comparison**: High-quality baseline for English
- **Custom encouragement**: Language-specific encouragement messages

### 4. Evaluation Framework
- **Comprehensive benchmarking**: Each provider tested on all three languages
- **Code-switch specific**: Special focus on mixed-language accuracy
- **Business impact analysis**: Real-world effectiveness across language scenarios
- **Continuous improvement**: Benchmarks tracked over time with new models

## Language-Specific Considerations

### Hindi Support
- **Strengths**: Large dataset, clear grammar, standard Devanagari script
- **Challenges**: Regional accents, homophones, context-dependent meanings
- **Best practices**: Multiple model variants for different dialects

### Gujarati Support
- **Strengths**: Phonetic consistency, clear script, logical grammar
- **Challenges**: Smaller dataset, fewer services offering Gujarati support
- **Solutions**: Custom training with business-specific vocabulary

### English Support
- **Strengths**: Largest dataset, clear context, multiple dialects
- **Challenges**: Accent diversity, idiomatic expressions
- **Integration**: Seamless fallback for mixed-language scenarios

### Code-Switching Edge Cases
- **Warning detection**: Identify when language switching becomes confusing
- **Clarification prompts**: Request repetition in consistent language
- **Escalation triggers**: Route to human when language confusion persists
- **Learning**: Track problematic patterns for system improvement

## Future Enhancements

1. **Real-time Translation**: Live interpretation between Hindi/Gujarati/English
2. **Regional Dialect Support**: Local variations (e.g., Rajasthani, Bhojpuri)
3. **Cultural Context**: Cultural nuance understanding in responses
4. **Sentiment Analysis**: Emotion detection across languages
5. **Voice Cloning**: Business-specific voice models for consistency
6. **Multi-modal Integration**: Text chat as alternative to voice
7. **AI4Bharat Integration**: Government API partnerships for content validation

## Security & Privacy

### Multilingual Data Protection
- Encrypted call recordings with language-specific metadata
- PII masking per language requirements
- GDPR-compliant with regional language considerations
- Opt-in for analytics with language consent

### Access Control
- Business-specific JWT tokens
- Role-based access control (admin, user)
- API rate limiting with language-specific thresholds
- HTTPS for all multilingual communications

### Consent and Ethics
- Call recording disclosure in caller's language
- Clear language options for preference setting
- Ethical AI usage guidelines for multilingual content
- Continuous improvement based on language equity metrics

## Implementation Roadmap

### Phase 1: Core Multilingual Pipeline (Week 1-2)
- Bhashini STT/TTS integration
- Language detection implementation
- Basic code-switching support
- Single-language conversation flow

### Phase 2: Advanced Language Features (Week 3-4)
- AI4Bharat provider integration
- Multi-provider benchmarking
- Code-switch evaluation harness
- Language-aware LLM agent

### Phase 3: Multi-language Dashboard (Week 5-6)
- Language analytics dashboard
- Multi-tenant language settings
- Quality monitoring per language
- Improvement tracking

### Phase 4: Production & Deployment (Week 7-8)
- Docker production setup
- CI/CD pipeline integration
- Performance optimization
- Documentation and training

## Team Collaboration

### Multilingual Testing Team
- Native speakers of Hindi, Gujarati, English
- Regional accent specialists
- Cultural context experts
- Language evaluation analysts

### Technical Team
- Multilingual API integration specialists
- Language model fine-tuning experts
- Audio processing engineers
- Quality assurance testers

### Business Team
- Regional business owner advisors
- Cultural compliance consultants
- User experience researchers
- Linguistic validation experts

This Vaani multilingual AI Phone Receptionist system provides a comprehensive solution for businesses serving linguistically diverse customer bases in India and beyond, with special emphasis on Hindi, Gujarati, and English language support and their natural combinations.

## Key Research Contributions

1. **Language Provider Benchmarking**: Comparative analysis of STT/TTS providers across Indian languages
2. **Code-Switch Accuracy Measurement**: First comprehensive metrics for mixed-language call handling
3. **Business Impact Analysis**: Real-world effectiveness data for multilingual customer service
4. **Performance Optimization**: Sub-500ms latency across all language scenarios
5. **Continual Improvement Framework**: System that learns from language-specific patterns and adapts

This system enables businesses to serve multilingual customer bases effectively, providing natural conversations in Hindi, Gujarati, English, and their combinations, while maintaining high performance standards and ethical AI practices.
