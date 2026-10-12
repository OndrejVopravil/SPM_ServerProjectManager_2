"""Pomocné funkce testů pro strom souborů."""


def find_item(tree, path):
    """Položka stromu podle cesty ('a/b/c.txt' nebo složka 'a/b')."""
    from PySide6.QtWidgets import QTreeWidgetItemIterator
    it = QTreeWidgetItemIterator(tree)
    while it.value():
        item = it.value()
        names, parent = [item.text(0)], item.parent()
        while parent:
            names.insert(0, parent.text(0))
            parent = parent.parent()
        if "/".join(names) == path:
            return item
        it += 1
    raise KeyError(path)
