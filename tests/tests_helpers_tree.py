"""Pomocné funkce testů pro strom souborů."""


def find_item(tree, path):
    """Položka stromu podle cesty ('a/b/c.txt' nebo složka 'a/b')."""
    from spm.gui.check_tree import all_items
    for item in all_items(tree):
        names, parent = [item.text(0)], item.parent()
        while parent:
            names.insert(0, parent.text(0))
            parent = parent.parent()
        if "/".join(names) == path:
            return item
    raise KeyError(path)
