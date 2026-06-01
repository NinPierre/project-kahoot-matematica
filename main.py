from flask import Flask, render_template, request, redirect, url_for, session
from blueprints.salas import salas_bp

# Criando a aplicação Flask
app = Flask(__name__)
app.config['SECRET_KEY'] = 'secret!'
app.register_blueprint(salas_bp, url_prefix='/salas')


# Rota principal
@app.route('/', methods=['GET', 'POST'])
def home():
    return render_template('home.html')


# Rota de registro
@app.route('/login', methods=['GET', 'POST'])
def login():
    return render_template('login.html')

# Rota de criação de sala
@app.route('/sala_create', methods=['GET', 'POST'])
def room_create():
    return render_template('room_create.html')






if __name__ == '__main__':
    app.run(debug=True)

