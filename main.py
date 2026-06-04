from flask import Flask, render_template, request, redirect, url_for, session

# Criando a aplicação Flask
app = Flask(__name__)
app.config['SECRET_KEY'] = 'secret!'


# Simulação simples de login (substituir por banco de dados real)
USUARIOS = {
    'professor': '1234'
}

def check_login(username, password):
    return USUARIOS.get(username) == password


# Rota principal
@app.route('/', methods=['GET', 'POST'])
def home():
    return render_template('home.html')


# Rota de login
@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None

    # Se já estiver logado, redireciona pro dashboard
    if 'user' in session:
        return redirect(url_for('home'))

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()

        if not username or not password:
            error = 'Preencha todos os campos.'
        elif check_login(username, password):
            session['user'] = username
            session.permanent = True
            return redirect(url_for('dashboard'))
        else:
            error = 'Usuário ou senha incorretos.'

    return render_template('login.html', error=error)


# Rota de logout
@app.route('/logout')
def logout():
    session.pop('user', None)
    return redirect(url_for('home'))


# Rota do dashboard (protegida)
@app.route('/dashboard')
def dashboard():
    if 'user' not in session:
        return redirect(url_for('login'))
    return render_template('DashBoard.html')


# Rota de criação de sala
@app.route('/sala_create', methods=['GET', 'POST'])
def room_create():
    if 'user' not in session:
        return redirect(url_for('login'))
    return render_template('home.html')  # Substituir por room_create.html quando criado


if __name__ == '__main__':
    app.run(debug=True)
