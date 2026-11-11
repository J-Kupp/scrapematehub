# Mundo fixtures

`listing.html` is a representative rendered catalogue page. Mundo renders catalogue
cards in the browser, so discovery uses Playwright and follows each category's next
page control.

`detail.html` contains the public product payload embedded in Mundo's React component
store. The parser extracts public catalogue information only. Product data sheet links
may point to Trustbox, but are retained as links rather than fetched because Trustbox
can require authentication.
