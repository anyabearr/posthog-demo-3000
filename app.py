import atexit
import logging
import time
import uuid
from datetime import datetime
from flask import Flask, render_template, redirect, url_for, flash, request, jsonify, make_response, session
from config import DevelopmentConfig
from models import db, User, Movie, MovieStats, BlogPost
from forms import SignupForm, LoginForm, ChangePlanForm, BlogPostForm
from flask_migrate import Migrate
from flask_login import LoginManager, current_user, login_user, logout_user, login_required
from flask_wtf.csrf import CSRFProtect
from posthog import Posthog, identify_context
from posthog.integrations.flask import PosthogFlaskIntegration

# Configure logging
logging.basicConfig(level=logging.DEBUG)

app = Flask(__name__)
app.config.from_object(DevelopmentConfig)

# Initialize CSRF protection
csrf = CSRFProtect(app)


class DisabledPosthog:
    """No-op client used when PostHog is not configured."""

    disabled = True

    def capture(self, *args, **kwargs):
        return None

    def shutdown(self):
        return None


posthog_key = app.config.get('PH_PROJECT_KEY')
posthog_host = app.config.get('PH_HOST')
posthog_log_logger = logging.getLogger('hogflix.posthog_logs')
posthog_log_logger.setLevel(logging.INFO)
posthog_log_logger.propagate = False


def configure_posthog_log_capture():
    """Export only HogFlix's purpose-written operational logs to PostHog."""
    if not posthog_key or not posthog_host:
        return

    try:
        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
        from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
    except ImportError:
        if app.debug:
            app.logger.warning(
                'PostHog log capture requires opentelemetry-api, opentelemetry-sdk, '
                'and opentelemetry-exporter-otlp'
            )
        return

    logger_provider = LoggerProvider()
    otlp_exporter = OTLPLogExporter(
        endpoint=f"{posthog_host.rstrip('/')}/i/v1/logs",
        headers={"Authorization": f"Bearer {posthog_key}"},
        timeout=3,
    )
    logger_provider.add_log_record_processor(
        BatchLogRecordProcessor(otlp_exporter, export_timeout_millis=3000)
    )
    posthog_log_logger.addHandler(LoggingHandler(logger_provider=logger_provider))
    atexit.register(logger_provider.shutdown)
    posthog_log_logger.info(
        'PostHog log capture configured',
        extra={'event': 'posthog_log_capture_configured'},
    )


if posthog_key and posthog_host:
    posthog = Posthog(
        posthog_key,
        host=posthog_host,
        enable_exception_autocapture=True,
        privacy_mode=False,
    )
    PosthogFlaskIntegration(app, client=posthog)
    atexit.register(posthog.shutdown)
    configure_posthog_log_capture()
else:
    posthog = DisabledPosthog()
    if app.debug:
        if not posthog_key:
            app.logger.warning(
                'PH_PROJECT_KEY variable required by PostHog is missing or un-configured, '
                'this causes events to be silently missed. This error stops appearing once '
                'PH_PROJECT_KEY is configured'
            )
        if not posthog_host:
            app.logger.warning(
                'PH_HOST variable required by PostHog is missing or un-configured, this '
                'causes events to be silently missed. This error stops appearing once PH_HOST '
                'is configured'
            )

db.init_app(app)
migrate = Migrate(app, db)

login = LoginManager(app)
login.login_view = 'login'


def bind_posthog_user(user, include_person_properties=False):
    """Bind the current request's PostHog context to a stable account id."""
    if getattr(posthog, 'disabled', False):
        return

    distinct_id = str(user.id)
    identify_context(distinct_id)
    if include_person_properties:
        posthog.set(
            distinct_id=distinct_id,
            properties={
                'email': user.email,
                'username': user.username,
                'plan': user.plan,
                'is_admin': user.is_admin,
            },
        )


def analytics_distinct_id():
    """Return the account id or a session-scoped id for anonymous visitors."""
    if current_user.is_authenticated:
        return str(current_user.id)

    if 'posthog_anonymous_id' not in session:
        session['posthog_anonymous_id'] = uuid.uuid4().hex
    return session['posthog_anonymous_id']


@app.before_request
def bind_authenticated_posthog_user():
    """Ensure captures and exceptions inherit the authenticated user context."""
    if current_user.is_authenticated:
        bind_posthog_user(current_user)


# --- LLM Chat setup (enabled when OPENAI_API_KEY is set) ---
chat_enabled = bool(app.config.get('OPENAI_API_KEY'))
openai_client = None
openai_instrumented = False
langchain_available = False
if chat_enabled:
    # Prefer LangChain for chat; fall back to direct OpenAI client
    try:
        from langchain_openai import ChatOpenAI  # type: ignore
        from langchain_core.messages import SystemMessage, HumanMessage, AIMessage  # type: ignore
        from posthog.ai.langchain import CallbackHandler  # type: ignore
        langchain_available = True
    except Exception:
        langchain_available = False

    if not langchain_available:
        try:
            from posthog.ai.openai import OpenAI as PHOpenAI  # type: ignore
            openai_client = PHOpenAI(api_key=app.config['OPENAI_API_KEY'], posthog_client=posthog)
            openai_instrumented = True
        except Exception:
            try:
                from openai import OpenAI as RawOpenAI  # type: ignore
                openai_client = RawOpenAI(api_key=app.config['OPENAI_API_KEY'])
            except Exception:
                openai_client = None
                chat_enabled = False

@login.user_loader
def load_user(id):
    return db.session.get(User, int(id))

@app.route('/')
def index():

    family_movies = Movie.query.filter_by(genre='Family').all()
    action_movies = Movie.query.filter_by(genre='Action').all()

    response = make_response(render_template(
        'index.html',
        family_movies=family_movies,
        action_movies=action_movies,
        chat_enabled=chat_enabled
    ))

    response.delete_cookie('posthog_js')

    return response


@app.route('/movie/<int:movie_id>')
def movie(movie_id):
    movie = Movie.query.get_or_404(movie_id)
    return render_template('movie.html', movie=movie)

def formatted_now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


PLAN_PRICES = {
    "Premium": 9.99,
    "Max-imal": 19.99,
    "Free": 0
}


@app.route('/signup', methods=['GET', 'POST'])
def signup():
    form = SignupForm()
    preselected_plan = request.args.get('plan')

    if request.method == 'POST':
        app.logger.debug(f"Form data received: {request.form}")
        plan = request.form.get('plan')  # Get plan from form data
        app.logger.debug(f"Plan value: {plan}")
        
        if form.validate_on_submit() and plan in ['Free', 'Premium', 'Max-imal']:
            user = User(
                username=form.username.data,
                email=form.email.data,
                plan=plan,
                is_adult=not form.is_adult.data  
            )
            user.set_password(form.password.data)
            db.session.add(user)
            db.session.commit()
            app.logger.debug(f"New user created: {user.username} with plan: {user.plan}")
            bind_posthog_user(user, include_person_properties=True)
            posthog.capture(
                'user_signed_up',
                properties={
                    'plan': plan,
                    'date_time': formatted_now()
                }
            )
            # Subscription purchase event for Revenue Analytics
            try:
                months = 1
                price_dollars = PLAN_PRICES.get(plan, 0)
                posthog.capture(
                    'subscription_purchased',
                    properties={
                        'plan': plan,
                        'months': months,
                        'price': int(round(price_dollars * 100)),
                        'currency': 'USD'
                    }
                )
            except Exception:
                app.logger.exception('Failed to capture subscription_purchased')
            flash('Congratulations, you are now a registered user!')
            return redirect(url_for('login'))
        else:
            if 'plan' not in request.form or not request.form['plan']:
                flash('Please select a plan.', 'error')
            app.logger.debug(f"Form validation failed. Errors: {form.errors}")
    
    return render_template('signup.html', form=form, preselected_plan=preselected_plan)


@app.route('/login', methods=['GET', 'POST'])

def login():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    
    form = LoginForm()
    
    if form.validate_on_submit():
        user = User.query.filter_by(username=form.username.data).first()
        
        if user is None or not user.check_password(form.password.data):
            flash('Invalid username or password', 'error')
            return redirect(url_for('login'))
        
        login_user(user, remember=True)
        bind_posthog_user(user, include_person_properties=True)
        posthog.capture(
            'user_logged_in',
            properties={
                'plan': user.plan,
                'date_time': formatted_now()
            }
        )

        flash('Welcome back!', 'success')
        
        next_page = request.args.get('next')
        return redirect(next_page or url_for('index'))
    
    return render_template('login.html', form=form)


@app.route('/logout')
def logout():
    if current_user.is_authenticated:
        posthog.capture(
            'user_logged_out',
            properties={
                'date_time': formatted_now()
            }
        )

    logout_user()
    flash('You have been logged out.')
    return redirect(url_for('index', reload='true'))


@app.route('/search', methods=['POST'])
def search():
    query = request.form.get('query')
    if not current_user.is_authenticated and not getattr(posthog, 'disabled', False):
        identify_context(analytics_distinct_id())
    posthog.capture('search_performed', properties={'query_length': len(query or '')})
    return redirect(url_for('search_results', query=query))

@app.route('/search_results')
def search_results():
    query = request.args.get('query')
    movies = Movie.query.filter(Movie.title.like(f'%{query}%')).all()
    return render_template('search_results.html', query=query, movies=movies)

@app.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    form = ChangePlanForm()

    if request.method == 'POST':
        new_plan = request.form.get('plan')
        app.logger.debug(f"Plan to update: {new_plan}")
        if new_plan in ['Free', 'Premium', 'Max-imal']:
            previous_plan = current_user.plan
            current_user.plan = new_plan
            try:
                db.session.commit()
            except Exception as e:
                db.session.rollback()
                app.logger.error(f"Error updating plan: {e}")
                flash('An error occurred while updating your plan. Please try again.', 'danger')
                if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                    return jsonify({'success': False})
            else:
                try:
                    posthog.set(
                        distinct_id=str(current_user.id),
                        properties={'plan': new_plan},
                    )
                    posthog.capture(
                        'subscription_plan_changed',
                        properties={
                            'previous_plan': previous_plan,
                            'new_plan': new_plan,
                        },
                    )
                except Exception:
                    app.logger.exception('Failed to capture subscription_plan_changed')
                posthog_log_logger.info(
                    'Subscription plan updated',
                    extra={
                        'event': 'subscription_plan_updated',
                        'previous_plan': previous_plan,
                        'new_plan': new_plan,
                    },
                )
                app.logger.debug(f"User plan updated to: {new_plan}")
                flash('Your subscription plan has been updated!')
                if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                    return jsonify({'success': True})
        else:
            flash('Please select a valid plan.', 'danger')
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({'success': False})
        
        if not request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return redirect(url_for('profile'))

    stats_week = [stat.to_dict() for stat in MovieStats.query.filter_by(user_id=current_user.id, time_frame='week').all()]
    stats_month = [stat.to_dict() for stat in MovieStats.query.filter_by(user_id=current_user.id, time_frame='month').all()]
    stats_year = [stat.to_dict() for stat in MovieStats.query.filter_by(user_id=current_user.id, time_frame='year').all()]

    return render_template('profile.html', user=current_user, form=form, stats_week=stats_week, stats_month=stats_month, stats_year=stats_year)
@app.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    if request.method == 'POST':
        pass
    return render_template('settings.html', user=current_user)

@app.route('/plans', methods=['GET'])
def plans():
    return render_template('plans.html', user=current_user)

@app.route('/fourohfour', methods=['GET'])
def fourohfour():
    return render_template('404.html', user=current_user)

@app.route('/blog')
def blog():
    posts = BlogPost.query.order_by(BlogPost.timestamp.desc()).all()
    return render_template('blog.html', posts=posts)

@app.route('/delete_post/<int:post_id>', methods=['POST'])
@login_required
def delete_post(post_id):
    if not current_user.is_admin:
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403

    post = BlogPost.query.get_or_404(post_id)
    db.session.delete(post)
    db.session.commit()
    try:
        posthog.capture(
            'blog_post_deleted',
            properties={'post_id': post_id},
        )
    except Exception:
        app.logger.exception('Failed to capture blog_post_deleted')
    return jsonify({'success': True})

@app.route('/create_post', methods=['GET', 'POST'])
@login_required
def create_post():
    form = BlogPostForm()
    if form.validate_on_submit():
        post = BlogPost(title=form.title.data, content=form.content.data)
        db.session.add(post)
        db.session.commit()
        try:
            posthog.capture(
                'blog_post_created',
                properties={'post_id': post.id},
            )
        except Exception:
            app.logger.exception('Failed to capture blog_post_created')
        posthog_log_logger.info(
            'Blog post created',
            extra={'event': 'blog_post_created', 'post_id': post.id},
        )
        flash('Blog post created successfully!', 'success')
        return redirect(url_for('blog'))
    return render_template('create_post.html', form=form)

@app.errorhandler(404)
def page_not_found(e):
    return render_template('404.html'), 404

@app.route('/toc')
def toc():
    return render_template('toc.html')

@app.route('/feature-flags')
@login_required  # Optional: restrict to logged-in users only
def feature_flags():
    return render_template('feature_flags.html')

# --- Chat routes ---
@app.route('/chat', methods=['GET'])
def chat_page():
    if not chat_enabled:
        return redirect(url_for('index'))
    return render_template('chat.html', chat_enabled=chat_enabled)


@app.route('/chat', methods=['POST'])
@csrf.exempt
def chat_api():
    if not chat_enabled:
        return jsonify({ 'error': 'Chat disabled' }), 400

    user_message = (request.get_json() or {}).get('message') or request.form.get('message')
    if not user_message:
        return jsonify({ 'error': 'Message required' }), 400

    # Establish a stable conversation id for analytics and memory
    if 'conversation_id' not in session:
        session['conversation_id'] = uuid.uuid4().hex
    conversation_id = session['conversation_id']

    distinct_id = analytics_distinct_id()
    if not getattr(posthog, 'disabled', False):
        identify_context(distinct_id)

    try:
        ai_text = None
        turn_id = uuid.uuid4().hex
        if 'history' not in session:
            session['history'] = []

        if langchain_available:
            # Build chat history for LangChain
            lc_messages = [
                SystemMessage(content=(
                    "You are HogFlix's helpful assistant. Keep answers concise and use GitHub-Flavored Markdown. "
                    "When appropriate, include code blocks fenced with the correct language."
                ))
            ]
            for m in session['history']:
                role = m.get('role')
                content = m.get('content')
                if role == 'user':
                    lc_messages.append(HumanMessage(content=content))
                elif role == 'assistant':
                    lc_messages.append(AIMessage(content=content))
            lc_messages.append(HumanMessage(content=user_message))

            model = ChatOpenAI(
                model="gpt-4o-mini",
                api_key=app.config['OPENAI_API_KEY'],
                temperature=0.2,
            )

            # PostHog LangChain callback for detailed analytics
            try:
                callback_handler = CallbackHandler(
                    client=posthog,
                    distinct_id=distinct_id,
                    trace_id=turn_id,
                    properties={"$ai_session_id": conversation_id},
                    privacy_mode=False,
                )
            except Exception:
                callback_handler = None
                app.logger.exception('Failed to configure PostHog LangChain callback')

            if callback_handler:
                result = model.invoke(lc_messages, config={"callbacks": [callback_handler]})
            else:
                result = model.invoke(lc_messages)

            ai_text = getattr(result, 'content', None) or str(result)
        else:
            if openai_client is None:
                return jsonify({ 'error': 'Chat disabled' }), 400
            # Fallback to direct OpenAI client
            generation_started_at = time.monotonic()
            input_tokens = None
            output_tokens = None
            posthog_ai_kwargs = (
                {
                    "posthog_distinct_id": distinct_id,
                    "posthog_trace_id": turn_id,
                    "posthog_properties": {"$ai_session_id": conversation_id},
                }
                if openai_instrumented else {}
            )
            if hasattr(openai_client, 'responses'):
                resp = openai_client.responses.create(
                    model="gpt-4o-mini",
                    input=[{"role": "user", "content": user_message}],
                    **posthog_ai_kwargs,
                )
                try:
                    ai_text = resp.output_text
                except Exception:
                    choice = getattr(resp, 'choices', [{}])[0] if hasattr(resp, 'choices') else None
                    ai_text = (choice.get('message', {}) or {}).get('content') if isinstance(choice, dict) else None
                usage = getattr(resp, 'usage', None)
                input_tokens = getattr(usage, 'input_tokens', None)
                output_tokens = getattr(usage, 'output_tokens', None)
            else:
                completion = openai_client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[{"role": "user", "content": user_message}],
                    **posthog_ai_kwargs,
                )
                ai_text = completion.choices[0].message.content
                usage = getattr(completion, 'usage', None)
                input_tokens = getattr(usage, 'prompt_tokens', None)
                output_tokens = getattr(usage, 'completion_tokens', None)

        if not ai_text:
            ai_text = "(no response)"

        # Update session history
        session['history'].append({"role": "user", "content": user_message})
        session['history'].append({"role": "assistant", "content": ai_text})

        # Only the raw-client fallback needs an explicit generation event.
        if not langchain_available and not openai_instrumented:
            try:
                posthog.capture(
                    '$ai_generation',
                    distinct_id=distinct_id,
                    properties={
                        '$ai_model': 'gpt-4o-mini',
                        '$ai_provider': 'openai',
                        '$ai_trace_id': turn_id,
                        '$ai_session_id': conversation_id,
                        '$ai_input': [{'role': 'user', 'content': user_message}],
                        '$ai_output_choices': [{'role': 'assistant', 'content': ai_text}],
                        '$ai_input_tokens': input_tokens,
                        '$ai_output_tokens': output_tokens,
                        '$ai_latency': time.monotonic() - generation_started_at,
                    },
                )
            except Exception:
                app.logger.exception('Failed to capture $ai_generation')

        posthog_log_logger.info(
            'Chat response generated',
            extra={
                'event': 'chat_response_generated',
                'input_length': len(user_message),
                'output_length': len(ai_text),
                'chat_implementation': 'langchain' if langchain_available else 'openai',
            },
        )
        return jsonify({ 'message': ai_text })
    except Exception as e:
        app.logger.exception('Chat API error')
        return jsonify({ 'error': 'Failed to generate response' }), 500

if __name__ == '__main__':
    # app.run(debug=True, host=app.config['APP_HOST'], port=app.config['APP_PORT'])
    app.run(debug=True)
