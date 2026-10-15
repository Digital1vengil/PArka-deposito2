// Tailwind compilado una vez (antes se compilaba en el navegador con cdn.tailwindcss.com en cada pantalla).
// Después de cambiar clases en las plantillas: ver "Estilos (Tailwind)" en README.md.
// Diseño igual a la app de cambios PARKA: negro, blanco y grises; estados en color (Inter).
// Se redefinen los grises "zinc" para que toda la app tome el tema sin tocar cada pantalla.
module.exports = {
  content: ["./app/templates/**/*.html", "./app/static/**/*.js", "./app/**/*.py"],
  theme: { extend: { fontFamily: { sans: ['Inter', 'system-ui', 'sans-serif'] }, colors: {
    zinc: { 50:'#ffffff', 100:'#111111', 200:'#111111', 300:'#333333', 400:'#6b6b6b', 500:'#6b6b6b', 600:'#8a8a8a',
            700:'#e4e4e4', 800:'#f1f1f1', 900:'#fafafa', 950:'#111111' },
    teal: { 50:'#f0fdf4', 200:'#bbf7d0', 300:'#15803d', 400:'#15803d', 500:'#16a34a', 600:'#15803d', 700:'#166534' },
    violeta: { DEFAULT:'#111111', 2:'#333333', bg:'#f1f1f1' },
    red: { 50:'#fef2f2', 200:'#fecaca', 300:'#b91c1c', 400:'#b91c1c', 600:'#b91c1c', 700:'#b91c1c', 800:'#f3c2c2', 950:'#fee2e2' },
    amber: { 300:'#b45309', 400:'#b45309', 600:'#f59e0b', 700:'#d97706', 800:'#fcd34d', 950:'#fef3c7' },
    green: { 400:'#15803d', 700:'#15803d', 800:'#bbf7d0', 950:'#dcfce7' },
    sky: { 800:'#1d4ed8', 950:'#dbeafe' },
  } } },
};
