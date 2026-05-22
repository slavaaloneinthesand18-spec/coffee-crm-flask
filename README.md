Markdown

This is a Flask web application designed as a CRM system for automating coffee shop operations.

## Getting Started

First, make sure you have Python installed, then install the required dependencies:

```bash
pip install flask flask_sqlalchemy werkzeug
```
Next, run the development server:

```bash
python app.py
```
Open http://127.0.0.1:5000 with your browser to see the result.

The application automatically initializes the local SQLite database file coffee_shop.db upon the first launch.


Project Structure

The project layout is organized as follows:

    app.py — The core application file containing backend logic, models, and routing.

    coffee_shop.db — Local transaction and inventory database.

    templates/ — Directory containing HTML user interfaces (index.html, login.html, admin.html, etc.).

Key Features

    Role-Based Access: Distinct interfaces and permissions for Admin and Barista roles.

    Inventory Control: Automatic real-time deduction of ingredients from stock upon checkout.

    Loyalty Program: Customer bonus balance points management tracked by phone number.
