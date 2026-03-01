from core.utils.path_utils import get_data_path


class PersonaManager:
    def __init__(self):
        import os

        self.persona_path = get_data_path() / "persona.txt"
        self.persona_str = ""
        self.persona_mtime = 0.0

        """init persona prompt"""
        self.reload_persona()

    def get_persona(self) -> str:
        """
        Get persona text
        :return: str
        """
        import os

        try:
            mtime = os.path.getmtime(self.persona_path)
            if mtime != self.persona_mtime:
                self.reload_persona()
        except FileNotFoundError:
            pass
        return self.persona_str

    def update_persona(self, text):
        import os

        self.persona_str = text
        with open(self.persona_path, "w", encoding="utf-8") as f:
            f.write(text)
        self.persona_mtime = os.path.getmtime(self.persona_path)

    def reload_persona(self):
        import os

        if not self.persona_path.exists():
            self.persona_path.write_text("")
        with open(self.persona_path, "r", encoding="utf-8") as f:
            persona = f.read()
        self.persona_str = persona
        self.persona_mtime = os.path.getmtime(self.persona_path)
